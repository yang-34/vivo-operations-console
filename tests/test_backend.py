import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app as application
import engine
from adapter import import_accounts, normalize_accounts
from conftest import wait_batch
from store import db, decrypt

LIVE={'X-Data-Mode':'live'}


def test_all_5000_accounts_enter_batch_regardless_of_page(client,monkeypatch):
    payload=[{'openid':f'user-{i}','vivotoken':f'token-{i}'} for i in range(5000)]
    result=client.post('/api/accounts/import',headers=LIVE,json=payload)
    assert result.status_code==200 and result.json['added']==5000
    assert client.get('/api/accounts?page_size=15',headers=LIVE).json['total']==5000
    monkeypatch.setattr(engine,'launch',lambda *args:None)
    result=client.post('/api/batches',headers=LIVE,json={'profile':'full'})
    assert result.status_code==200
    with db('live') as c:
        assert c.execute('SELECT count(*) FROM runs WHERE batch_id=?',(result.json['id'],)).fetchone()[0]==5000


def test_import_atomic_upsert_and_credentials_never_exposed(client):
    payload=[{'openid':'account-1','nickname':'Alice','vivotoken':'private-token'}]
    assert client.post('/api/accounts/import',headers=LIVE,json=payload).json['added']==1
    payload[0]['nickname']='Alice Updated'
    assert client.post('/api/accounts/import',headers=LIVE,json=payload).json['updated']==1
    failed=client.post('/api/accounts/import',headers=LIVE,json=[{'openid':'new-id'},{'nickname':'invalid'}])
    assert failed.status_code==400
    listing=client.get('/api/accounts',headers=LIVE)
    assert listing.json['total']==1
    assert 'private-token' not in listing.get_data(as_text=True)
    assert 'credentials' not in listing.get_data(as_text=True)
    assert b'private-token' not in client.get('/api/accounts/export',headers=LIVE).data
    with db('live') as c:
        encrypted=c.execute('SELECT credentials FROM accounts').fetchone()[0]
        assert 'private-token' not in encrypted
        assert decrypt(encrypted)['vivotoken']=='private-token'


def test_demo_and_live_are_isolated(client):
    assert client.get('/api/accounts').json['total']==4000
    assert client.get('/api/accounts',headers=LIVE).json['total']==0
    assert client.post('/api/accounts/discover',json={}).status_code==400
    assert client.post('/api/accounts/sync-pools',json={}).status_code==400


def test_rank_pagination_and_csv_formula_safety(client):
    import_accounts('live',[{'openid':'formula','nickname':'=HYPERLINK("x")','vivotoken':'t'},{'openid':'other','vivotoken':'s'}])
    with db('live') as c:
        c.execute('UPDATE accounts SET points=500 WHERE external_id=?',('other',))
    result=client.get('/api/accounts?sort=points&page_size=1',headers=LIVE).json
    assert result['total']==2 and len(result['items'])==1
    assert result['items'][0]['external_id']=='other'
    exported=client.get('/api/accounts/export',headers=LIVE).data.decode('utf-8-sig')
    assert "'=HYPERLINK" in exported


def test_queue_continues_after_expired_and_disabled_account(client):
    result=client.post('/api/batches',json={'account_ids':[1,2,67,191],'profile':'full','concurrency':2})
    batch=wait_batch(client,result.json['id'])
    assert batch['total']==4 and batch['completed']==2 and batch['failed']==1 and batch['skipped']==1
    rows=client.get(f"/api/batches/{batch['id']}/runs").json['items']
    assert len(rows)==4 and all(r['finished_at'] for r in rows)
    good=next(r for r in rows if r['account_id']==1)
    assert len(client.get(f"/api/runs/{good['id']}/steps").json['items'])==7
    retry=client.post('/api/batches',json={'retry_batch':batch['id']})
    retried=wait_batch(client,retry.json['id'])
    assert retried['total']==1 and retried['failed']==1


def test_daily_ledger_prevents_duplicate_rewards(client):
    first=client.post('/api/batches',json={'account_ids':[1],'profile':'full'}).json['id']
    wait_batch(client,first)
    points=client.get('/api/accounts/1').json['account']['points']
    second=client.post('/api/batches',json={'account_ids':[1],'profile':'full'}).json['id']
    repeated=wait_batch(client,second)
    assert repeated['skipped']==1
    assert client.get('/api/accounts/1').json['account']['points']==points


def test_pause_resume_and_stop_preserve_every_account(client):
    result=client.post('/api/batches',json={'account_ids':list(range(1,31)),'profile':'full','concurrency':1})
    bid=result.json['id']
    assert client.post(f'/api/batches/{bid}/pause',json={}).status_code==200
    assert client.post('/api/batches',json={}).status_code==400
    assert client.post(f'/api/batches/{bid}/resume',json={}).status_code==200
    assert client.post(f'/api/batches/{bid}/stop',json={}).status_code==200
    batch=wait_batch(client,bid)
    assert batch['status']=='stopped'
    assert batch['completed']+batch['failed']+batch['skipped']+batch['cancelled']==30


def test_redeem_is_idempotent_and_updates_balance_stock(client):
    aid=client.get('/api/accounts?sort=points&page_size=1').json['items'][0]['id']
    before=client.get(f'/api/accounts/{aid}').json['account']['points']
    products=client.get('/api/products').json['items']
    stock=next(r['stock'] for r in products if r['id']=='demo-coffee')
    payload={'account_id':aid,'product_id':'demo-coffee','idempotency_key':'repeated-request'}
    first=client.post('/api/redemptions',json=payload)
    second=client.post('/api/redemptions',json=payload)
    assert first.json['status']=='success' and first.json['id']==second.json['id']
    assert client.get(f'/api/accounts/{aid}').json['account']['points']==before-680
    assert next(r['stock'] for r in client.get('/api/products').json['items'] if r['id']=='demo-coffee')==stock-1


def test_parallel_redemptions_cannot_overspend(client):
    with db('demo') as c:
        c.execute('UPDATE accounts SET points=680 WHERE id=1')
    def submit(key):
        with application.app.test_client() as parallel:
            import store
            result=parallel.post('/api/auth/login',json={'password':(store.DATA/'admin-password.txt').read_text().strip()})
            parallel.environ_base['HTTP_X_CSRF_TOKEN']=result.json['csrf_token']
            return parallel.post('/api/redemptions',json={'account_id':1,'product_id':'demo-coffee','idempotency_key':key}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses=list(pool.map(submit,['order-a','order-b']))
    assert sorted(statuses)==[200,400]
    assert client.get('/api/accounts/1').json['account']['points']==0


def test_redeem_failure_restores_but_uncertain_order_reserves(client,monkeypatch):
    from contextlib import nullcontext
    import commerce
    import_accounts('live',[{'openid':'live-account','vivotoken':'token'}])
    with db('live') as c:
        c.execute("UPDATE accounts SET points=2000,points_synced_at='2026-10-16T00:00:00+08:00'")
        c.execute("INSERT INTO products(id,name,category,price,stock,source,updated_at) VALUES('p','Coffee','生活礼券',680,5,'reference','today')")
    monkeypatch.setattr(commerce,'lease',lambda *a,**k:nullcontext(None))
    monkeypatch.setattr(commerce,'preview_with_proxy',lambda *a:{'points':2000,'product':{'price':680,'stock':5,'metadata':{}}})
    monkeypatch.setattr(commerce,'live_events',lambda *a,**k:iter([{'type':'exchange','status':'failed','message':'not available'}]))
    result=client.post('/api/redemptions',headers=LIVE,json={'account_id':1,'product_id':'p','idempotency_key':'fail'})
    assert result.json['status']=='failed'
    assert client.get('/api/accounts/1',headers=LIVE).json['account']['points']==2000
    monkeypatch.setattr(commerce,'live_events',lambda *a,**k:iter([{'type':'exchange_submit'}]))
    result=client.post('/api/redemptions',headers=LIVE,json={'account_id':1,'product_id':'p','idempotency_key':'unknown'})
    assert result.json['status']=='unknown'
    assert client.get('/api/accounts/1',headers=LIVE).json['account']['points']==1320
    assert client.post('/api/redemptions',headers=LIVE,json={'account_id':1,'product_id':'p','idempotency_key':'blocked'}).status_code==400


def test_cross_site_writes_blocked_and_invalid_mode_rejected(client):
    assert client.post('/api/batches',json={},headers={'Origin':'https://unrelated.example'}).status_code==403
    assert client.get('/api/accounts',headers={'X-Data-Mode':'invalid'}).status_code==400


def test_recovery_keeps_interrupted_runs_and_orders(client):
    with db('live') as c:
        c.execute("INSERT INTO batches VALUES('interrupted','test','full','running',0,1,'now',NULL)")
    engine.recover('live')
    assert client.get('/api/batches',headers=LIVE).json['items'][0]['status']=='paused'


def test_live_bridge_classifies_partial_and_failed_results():
    from worker import classify
    assert classify({'retcode':0})=='completed'
    assert classify({'retcode':1034})=='failed'
    assert classify({'results':[{'res':'error'}]})=='failed'
    assert classify({'completed':3,'no_change':1})=='partial'
    assert classify({'skip':'no activity'})=='skipped'


def test_unknown_order_reconciliation_refunds_only_once(client,monkeypatch):
    from contextlib import nullcontext
    import commerce
    import_accounts('live',[{'openid':'live-account','vivotoken':'token'}])
    with db('live') as c:
        c.execute("UPDATE accounts SET points=2000,points_synced_at='2026-10-16T00:00:00+08:00'")
        c.execute("INSERT INTO products(id,name,category,price,stock,source,updated_at) VALUES('p','Coffee','生活礼券',680,5,'reference','today')")
    monkeypatch.setattr(commerce,'lease',lambda *a,**k:nullcontext(None))
    monkeypatch.setattr(commerce,'preview_with_proxy',lambda *a:{'points':2000,'product':{'price':680,'stock':5,'metadata':{}}})
    monkeypatch.setattr(commerce,'live_events',lambda *a,**k:iter([{'type':'exchange_submit'}]))
    order=client.post('/api/redemptions',headers=LIVE,json={'account_id':1,'product_id':'p','idempotency_key':'unknown'}).json
    assert order['status']=='unknown'
    path=f"/api/redemptions/{order['id']}/reconcile"
    assert client.post(path,headers=LIVE,json={'status':'failed','note':'已在原平台确认无订单且积分未扣除'}).status_code==200
    assert client.get('/api/accounts/1',headers=LIVE).json['account']['points']==2000
    assert client.post(path,headers=LIVE,json={'status':'failed','note':'重复登记核实结果说明'}).status_code==400


def test_pool_sync_keeps_all_accounts_and_reports_failed_sources(client,monkeypatch):
    import_accounts('live',[{'openid':'known-vivo','vivotoken':'token','wx_openid':'wx-known'}])
    events=[{'type':'pool_start','total':3},
      {'type':'pool_accounts','pool_index':1,'items':[{'openid':f'wx:w-{i}','_wx_openid':f'w-{i}','_pool':'http://localhost:9','panel_status':'offline'} for i in range(101)]},
      {'type':'pool_accounts','pool_index':2,'items':[{'openid':'wx:wx-known','_wx_openid':'wx-known','_pool':'http://localhost:9'},{'openid':'wx:w-0','_wx_openid':'w-0','_pool':'http://localhost:9'}]},
      {'type':'pool_error','pool_index':3},{'type':'pool_end'}]
    monkeypatch.setattr(engine,'live_events',lambda *a,**k:iter(events))
    result=client.post('/api/accounts/sync-pools',headers=LIVE,json={})
    assert result.status_code==200
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        job=client.get('/api/imports',headers=LIVE).json['items'][0]
        if job['status']!='running':
            break
        time.sleep(.02)
    assert job['status']=='partial' and job['failed']==1 and job['processed']==3
    listing=client.get('/api/accounts',headers=LIVE).json
    assert listing['total']==102 and listing['counts']['pending_login']==101
