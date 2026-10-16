import json
from types import SimpleNamespace

import pytest
from store import db,encrypt,decrypt,now


def test_maintenance_requires_explicit_selection_and_never_starts_earning(client,monkeypatch):
    import engine
    calls=[]
    monkeypatch.setattr(engine,'launch',lambda mode,bid:calls.append(bid))
    with db() as c:c.execute('INSERT INTO accounts(external_id,nickname,created_at) VALUES(?,?,?)',('owned','Owned',now()))
    for body in ({'profile':'login'},{'profile':'full','account_ids':[1]},{'profile':'login','account_ids':[]}):
        assert client.post('/api/accounts/maintenance',headers={'X-Data-Mode':'live'},json=body).status_code==400
    with db() as c:c.execute("INSERT INTO batches VALUES('existing','Full','full','paused',1,1,?,NULL)",(now(),))
    result=client.post('/api/accounts/maintenance',headers={'X-Data-Mode':'live'},json={'profile':'app_check','account_ids':[1],'concurrency':99})
    assert result.status_code==200 and calls==[result.json['id']]
    with db() as c:
        assert c.execute("SELECT status FROM batches WHERE id='existing'").fetchone()[0]=='paused'
        row=c.execute('SELECT profile,total,concurrency FROM batches WHERE id=?',(result.json['id'],)).fetchone()
        assert tuple(row)==('app_check',1,8)


def test_panel_key_is_routed_to_its_own_account_only(client,monkeypatch):
    from engine import live_events
    import protocol.runner
    import hashlib
    base='http://panel.test:8080';fp=hashlib.sha256(base.encode()).hexdigest()
    with db() as c:
        c.execute('INSERT INTO panel_sources(fingerprint,url,label,api_key) VALUES(?,?,?,?)',(fp,encrypt({'url':base}),'Panel',encrypt({'key':'private-panel-key'})))
        c.execute("UPDATE settings SET value='true' WHERE key='public_proxy_auth_approved'")
    seen=[]
    def events(payload):seen.append(payload);yield {'type':'result','points':1}
    monkeypatch.setattr(protocol.runner,'iter_events',events)
    list(live_events('live',{'id':1,'nickname':'Owned','credentials':encrypt({'_pool':base})},action='login',proxy={}))
    assert seen[0]['panel_api_key']=='private-panel-key'
    list(live_events('live',{'id':2,'nickname':'Other','credentials':encrypt({'_pool':'http://other.test'})},action='login',proxy={}))
    assert 'panel_api_key' not in seen[1]


def test_app_check_without_session_never_renews_or_sends_exchange(client,monkeypatch):
    from protocol import runner
    monkeypatch.setattr(runner,'renew',lambda *a:pytest.fail('Missing App cookie must not be replaced with mini-app token'))
    events=list(runner.iter_events({'mode':'live','account_id':1,'account':{'openid':'owned','vivotoken':'mini'},'action':'app_session_check','profile':'app_check'}))
    assert len(events)==1 and events[0]['type']=='error' and '未导入' in events[0]['message']


def test_renewal_rejects_identity_change():
    from protocol.auth import renew
    from protocol.transport import AccountStop
    calls=[]
    class Session:
        def post(self,url,**kwargs):
            calls.append(url)
            value={'code':0,'data':{'result':{'code':'one-time'}}} if url.endswith('/wxapp/getCode') else {'code':0,'data':{'openid':'other','vivotoken':'new','needBindPhone':'0'}}
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:value)
    with pytest.raises(AccountStop):renew({'openid':'owned','_pool':'https://panel.test'},Session())
    assert len(calls)==2


def test_app_check_updates_session_and_not_task_income(client,monkeypatch):
    import engine
    from app_sessions import import_session
    from test_app_sessions import payload
    aid=import_session('live',{'input':payload(),'create_account':True})['account_id']
    with db() as c:
        c.execute("UPDATE accounts SET gained=99,task_status='partial' WHERE id=?",(aid,))
        c.execute("INSERT INTO batches VALUES('check','Check','app_check','running',1,1,?,NULL)",(now(),))
        rid=c.execute("INSERT INTO runs(batch_id,account_id,status) VALUES('check',?,'running')",(aid,)).lastrowid
        version=c.execute('SELECT version FROM app_sessions WHERE account_id=?',(aid,)).fetchone()[0]
    def events(*a,**kw):
        assert kw['action']=='app_session_check' and kw['refresh_login'] is False
        yield {'type':'app_session','points':42,'session_version':version}
        yield {'type':'result','points':42,'before':42,'gained':0}
    monkeypatch.setattr(engine,'live_events',events)
    engine.run_one('live',rid,'app_check',proxy_override={})
    with db() as c:
        assert c.execute('SELECT status FROM app_sessions').fetchone()[0]=='verified'
        assert tuple(c.execute('SELECT points,gained,task_status FROM accounts').fetchone())==(42,99,'partial')
        assert c.execute('SELECT count(*) FROM point_history').fetchone()[0]==0
