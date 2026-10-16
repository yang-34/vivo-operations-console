import json
import time
import pytest
import app as application
from store import db,now
from adapter import import_accounts
from protocol.tasks import execute_center
from proxies import parse_candidate
from executor import claim


def test_authentication_protects_pages_static_and_all_business_interfaces(client):
    anonymous=application.app.test_client()
    for path in ('/','/favicon.svg','/anything'):
        assert anonymous.get(path).status_code==302
    for path in ('/api/health','/api/accounts','/api/proxies','/api/panels','/api/engine','/assets/test.js'):
        assert anonymous.get(path).status_code==401
    assert anonymous.get('/login').status_code==200
    assert anonymous.post('/api/batches',json={}).status_code==401


def test_session_cookie_and_csrf_are_enforced(client):
    assert client.get('/api/auth/session').status_code==200
    assert client.post('/api/panels',json={'label':'x','url':'http://localhost:9000'},headers={'X-CSRF-Token':'invalid'}).status_code==403
    response=client.post('/api/auth/logout',json={})
    assert response.status_code==200
    assert client.get('/api/proxies').status_code==401


def test_panel_creation_filtering_and_duplicate_detection(client):
    headers={'X-Data-Mode':'live'}
    result=client.post('/api/panels',headers=headers,json={'label':'Test source','url':'http://127.0.0.1:9999','api_key':'test-only'})
    assert result.status_code==200
    assert client.post('/api/panels',headers=headers,json={'label':'duplicate','url':'http://127.0.0.1:9999/'}).status_code==400
    with db('live') as c:c.execute("UPDATE panel_sources SET status='connected',online_count=10,offline_count=2")
    listing=client.get('/api/panels?status=online',headers=headers).json
    assert listing['total']==1 and listing['items'][0]['online_count']==10
    assert 'test-only' not in json.dumps(listing)


def test_proxy_bulk_format_plaintext_credentials_expiry_and_pagination(client):
    headers={'X-Data-Mode':'live'}
    response=client.post('/api/proxies/import',headers=headers,json={'urls':'8.8.8.8|9125|test-user|test-password|2026-10-21\ninvalid-line','check':False})
    assert response.status_code==200 and response.json['added']==1 and len(response.json['errors'])==1
    listing=client.get('/api/proxies?page_size=1',headers=headers).json
    assert listing['items'][0]['username']=='test-user' and listing['items'][0]['password']=='test-password'
    with db('live') as c:assert 'test-password' in c.execute('SELECT url FROM proxies').fetchone()[0]
    assert parse_candidate('1.1.1.1|1080|a|b|2026-10-21')['expires_at'].startswith('2026-10-22')


def test_transactional_claim_never_shares_an_exit(client):
    import_accounts('live',[{'openid':f'account-{i}','vivotoken':'test'} for i in range(3)])
    with db('live') as c:
        c.execute("UPDATE settings SET value='true' WHERE key='proxy_required'")
        c.execute("INSERT INTO proxies(fingerprint,url,source,status,exit_ip,created_at) VALUES('a','socks5h://8.8.8.8:1080','user','healthy','8.8.8.8',?)",(now(),))
        c.execute("INSERT INTO batches VALUES('claim','test','full','running',3,3,?,NULL)",(now(),))
        c.executemany("INSERT INTO runs(batch_id,account_id) VALUES('claim',?)",[(1,),(2,),(3,)])
    first=claim('live','claim',3)
    assert first and claim('live','claim',3) is None
    with db('live') as c:
        assert c.execute('SELECT count(*) FROM execution_leases').fetchone()[0]==2
        c.execute('DELETE FROM execution_leases')
    assert claim('live','claim',3)[0]!=first[0]


def test_task_center_deduplicates_identical_writes_and_verifies_in_batches():
    class FakeApi:
        def __init__(self):self.completed=set();self.reads=0;self.writes=0
        def h5_call(self,host,path,**kwargs):
            self.reads+=1
            return {'retcode':0,'data':{'tasks':[{'hasMore':False,'list':[{'id':i,'name':f'Task {i}','taskComplete':i in self.completed,'completedTimes':int(i in self.completed)} for i in range(10)]}]}}
        def _tasks_for_pkg(self,pkg):raise AssertionError('legacy 0..6 enumeration should not be used')
        def sign_biz_names(self):return {}
        def complete_task(self,task,mapping):
            self.writes+=1;self.completed.add(task['id'])
            self._tasks_for_pkg(task['_pkg']) # Legacy verification is suppressed here.
    api=FakeApi();result=execute_center(api,['one','two','three'])
    assert result['completed']==10 and result['unique']==10 and result['discovered']==30
    assert api.writes==10 and api.reads==4


def test_incomplete_task_is_not_counted_as_completed():
    class FakeApi:
        def h5_call(self,*a,**k):return {'retcode':0,'data':{'tasks':[{'list':[{'id':1,'name':'blocked','taskComplete':False,'completedTimes':0}]}]}}
        def _tasks_for_pkg(self,pkg):return {}
        def sign_biz_names(self):return {}
        def complete_task(self,*a):pass
    result=execute_center(FakeApi(),['one','two'])
    assert result['completed']==0 and result['no_change']==1


def test_mixed_step_results_and_secrets_are_logged_accurately():
    from protocol.runner import classify,safe_log
    assert classify([{'code':0},{'code':123}])=='partial'
    assert classify([{'code':0},{'code':20041}])=='completed'
    result=safe_log({'token':'must-not-appear','result':{'_err':'url?token=must-not-appear'}})
    assert 'must-not-appear' not in json.dumps(result)


def test_native_protocol_does_not_depend_on_reference_directory():
    from worker import load_reference
    from pathlib import Path
    module=load_reference('/a/path/that/does/not/exist')
    assert Path(module.__file__).parent.name=='protocol'
    assert not hasattr(module.VivoApi,'AI_KEY_DEF')


def test_string_false_is_not_mistaken_for_a_completed_task():
    from protocol.tasks import is_complete
    assert not is_complete({'taskComplete':'false'})
    assert not is_complete({'taskComplete':'0'})
    assert is_complete({'taskComplete':'true'})
    assert is_complete({'taskComplete':True})


def test_failed_proxy_is_removed_from_new_dispatch(client,monkeypatch):
    import engine,protocol.runner,hashlib
    from store import encrypt
    url='socks5h://8.8.8.8:1080'
    with db('live') as c:
        c.execute("UPDATE settings SET value='true' WHERE key='public_proxy_auth_approved'")
        c.execute("INSERT INTO proxies(fingerprint,url,source,status,exit_ip,created_at) VALUES(?,?,'user','healthy','8.8.8.8',?)",(hashlib.sha256(url.encode()).hexdigest(),url,now()))
    monkeypatch.setattr(protocol.runner,'iter_events',lambda payload:iter([{'type':'error','message':'代理连续三次连接失败','network_failure':True}]))
    list(engine.live_events('live',{'id':1,'credentials':encrypt({'openid':'test','vivotoken':'test'}),'nickname':'Test'},proxy={'http':url,'https':url}))
    with db('live') as c:assert c.execute('SELECT status FROM proxies').fetchone()[0]=='failed'


def test_server_settings_do_not_require_the_original_directory(client):
    headers={'X-Data-Mode':'live'}
    before=client.get('/api/settings',headers=headers).json
    response=client.put('/api/settings',headers=headers,json={'worker_processes':24,'reference_path':''})
    assert response.status_code==200
    after=client.get('/api/settings',headers=headers).json
    assert after['worker_processes']==24 and after['concurrency']==before['concurrency']
    assert after['reference_path']=='' and after['adapter_available']


def test_claim_covers_accounts_beyond_unavailable_bound_prefix(client):
    import_accounts('live',[{'openid':f'coverage-{i}','vivotoken':'test'} for i in range(550)])
    with db('live') as c:
        c.execute("UPDATE settings SET value='false' WHERE key='proxy_auto_switch'")
        c.execute("INSERT INTO proxies(fingerprint,url,source,status,exit_ip,created_at) VALUES('bad','socks5h://1.1.1.1:1080','user','failed','1.1.1.1',?)",(now(),))
        c.execute("INSERT INTO proxies(fingerprint,url,source,status,exit_ip,created_at) VALUES('good','socks5h://8.8.8.8:1080','user','healthy','8.8.8.8',?)",(now(),))
        c.executemany('INSERT INTO proxy_bindings VALUES(?,1,?)',[(i,now()) for i in range(1,520)])
        c.execute("INSERT INTO batches VALUES('coverage','test','full','running',550,20,?,NULL)",(now(),))
        c.executemany("INSERT INTO runs(batch_id,account_id) VALUES('coverage',?)",[(i,) for i in range(1,551)])
    result=claim('live','coverage',20)
    with db('live') as c:assert c.execute('SELECT account_id FROM runs WHERE id=?',(result[0],)).fetchone()[0]==520


def test_resume_all_includes_new_accounts_but_selected_scope_stays_fixed(client,monkeypatch):
    import engine
    monkeypatch.setattr(engine,'launch',lambda *args:None)
    import_accounts('live',[{'openid':'one','vivotoken':'test'}])
    bid=engine.create_batch('live',start_paused=True)
    import_accounts('live',[{'openid':'two','vivotoken':'test'}])
    engine.control('live',bid,'resume')
    with db('live') as c:
        assert c.execute('SELECT total FROM batches WHERE id=?',(bid,)).fetchone()[0]==2
        c.execute("UPDATE batches SET status='completed' WHERE id=?",(bid,))
    selected=engine.create_batch('live',ids=[1],start_paused=True)
    import_accounts('live',[{'openid':'three','vivotoken':'test'}])
    engine.control('live',selected,'resume')
    with db('live') as c:assert c.execute('SELECT total FROM batches WHERE id=?',(selected,)).fetchone()[0]==1
