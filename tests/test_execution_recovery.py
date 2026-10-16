import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from store import db, decrypt, encrypt, now, today


def test_automatic_renewal_excludes_offline_and_backs_off_failures(client):
    from execution_policy import renewal_candidates
    stamp = datetime.fromisoformat(now()).timestamp()
    def account(aid, panel='online', saved=0):
        return {'id': aid, 'status': 'pending_login', 'panel_status': panel,
                'credentials': encrypt({'_pool': 'https://owned.test', 'saved_at': saved})}
    accounts = [account(1, 'offline'), account(2), account(3), account(4)]
    latest = {2: {'status': 'failed', 'finished_at': now(), 'error': 'HTTPError'},
              3: {'status': 'failed', 'finished_at': (datetime.fromisoformat(now()) - timedelta(hours=2)).isoformat(), 'error': '需要验证'}}
    assert renewal_candidates(accounts, latest, stamp) == [4]
    assert renewal_candidates([account(2, saved=stamp+1)], latest, stamp+2) == [2]


def test_offline_account_never_requests_login_code():
    from protocol.auth import renew
    from protocol.transport import AccountStop
    session = Mock()
    with pytest.raises(AccountStop) as caught:
        renew({'_pool': 'https://owned.test', '_panel_status': 'offline'}, session)
    assert caught.value.diagnostics['reason_kind'] == 'panel_offline'
    session.post.assert_not_called()


def test_panel_http_failure_has_explicit_safe_diagnostics():
    from protocol.auth import renew
    from protocol.transport import AccountStop
    response = SimpleNamespace(status_code=404)
    with pytest.raises(AccountStop) as caught:
        renew({'_pool': 'https://owned.test', 'openid': 'own'}, SimpleNamespace(post=lambda *a, **k: response))
    assert caught.value.provider_code == 'HTTP_404'
    assert caught.value.diagnostics['endpoint'] == '/wxapp/getCode'
    assert 'owned.test' not in str(caught.value)


def test_account_verification_retains_actual_provider_message(monkeypatch):
    from protocol.transport import AccountStop, Transport
    response = requests.Response(); response.status_code = 200
    response._content = json.dumps({'code': 1050, 'message': '账户异常，请联系客服'}).encode()
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: response)
    with pytest.raises(AccountStop) as caught:
        Transport(gap=0).post('https://sign.vivo.com.cn/api/sign/dosign.do')
    assert caught.value.provider_code == '1050'
    assert caught.value.diagnostics['provider_message'] == '账户异常，请联系客服'
    assert caught.value.diagnostics['reason_kind'] == 'account_verification'


def test_verification_hold_survives_restart_and_blocks_all_network(client, monkeypatch):
    from engine import live_events
    from execution_policy import record_verification_hold, verification_hold
    import proxies
    raw = {'openid': 'own', 'vivotoken': 'old-token'}
    event = {'message': '要求验证', 'provider_code': '1050', 'diagnostics': {'reason_kind': 'account_verification'}}
    record_verification_hold('live', 1, raw, event)
    assert verification_hold('live', 1, raw)['provider_code'] == '1050'
    monkeypatch.setattr(proxies, 'lease', lambda *a: pytest.fail('held account must not acquire a proxy'))
    events = list(live_events('live', {'id': 1, 'credentials': encrypt(raw)}, action='tasks'))
    assert events[0]['type'] == 'error' and events[0]['retryable'] is False
    assert verification_hold('live', 1, {**raw, 'vivotoken': 'new-token'}) is None


def test_coupon_explicit_activity_rejection_uses_form_and_readback(client, monkeypatch):
    from protocol import har_tasks
    from protocol.taskflow.protocol_common import ActionRequired
    ctx = SimpleNamespace(mode='live', account_id=1, credentials={'scopes': {}},
        persist_scopes=lambda value: None, ensure_allowed=lambda: None, transport=SimpleNamespace(observations=[]))
    monkeypatch.setattr(har_tasks, 'VivoClient', lambda *a: None)
    monkeypatch.setattr(har_tasks, 'BENEFITS', ())
    monkeypatch.setattr(har_tasks, 'COUPONS', (383,))
    methods, received = [], [False]
    class Native:
        def __init__(self, *a): pass
        def shop_session(self): pass
        def call(self, host, path, **kwargs):
            if path == '/member/query': return {'level': 1}
            if path == '/coupon/v2/queryCouponActivity':
                return {'couponModule': {'activityUuid': 'current'}, 'couponModuleItems': [
                    {'couponUuid': 'coupon', 'couponName': 'free', 'couponType': 2, 'receiveStatus': 211201, 'couponFlag': int(received[0])}]}
            methods.append('form' if 'form' in kwargs else 'json')
            if 'form' not in kwargs:
                raise ActionRequired('活动无效', provider_code='211415', reason_kind='provider_rejected')
            received[0] = True
            return {}
    monkeypatch.setattr(har_tasks, 'Native', Native)
    result = har_tasks.membership(SimpleNamespace(source_context=ctx))
    assert methods == ['json', 'form'] and result['results'][0]['status'] == 'completed'
    har_tasks.membership(SimpleNamespace(source_context=ctx))
    assert methods == ['json', 'form']


def test_persisted_step_selection_reaches_executor_without_marking_full_done(client, monkeypatch):
    import engine
    from adapter import import_accounts
    monkeypatch.setattr(engine, 'launch', lambda *a: None)
    import_accounts('live', [{'openid': 'scoped', 'vivotoken': 'token'}])
    with db('live') as conn:
        aid=conn.execute('SELECT id FROM accounts').fetchone()[0]
    with pytest.raises(ValueError):
        engine.create_batch('live', only_steps=['unknown'])
    bid=engine.create_batch('live', ids=[aid], only_steps=['har.membership'])
    with db('live') as conn:
        rid=conn.execute('SELECT id FROM runs WHERE batch_id=?',(bid,)).fetchone()[0]
    def events(*a, **kwargs):
        assert kwargs['only_steps']==['har.membership']
        yield {'type':'result','before':100,'points':100,'gained':0}
    monkeypatch.setattr(engine,'live_events',events)
    engine.run_one('live',rid,None,proxy_override={})
    with db('live') as conn:
        assert conn.execute('SELECT count(*) FROM ledger').fetchone()[0]==0


def test_queued_workflow_resumes_after_predecessor_only(client, monkeypatch):
    import workflow
    with db('live') as conn:
        conn.execute("INSERT INTO batches VALUES('before','before','full','running',0,1,?,NULL)",(now(),))
        conn.execute("INSERT INTO batches VALUES('after','after','full','paused',0,1,?,NULL)",(now(),))
        conn.execute("INSERT INTO workflows VALUES('queue','active','after','waiting_batch:before','waiting',?,?)",(now(),now()))
    calls=[]
    monkeypatch.setattr(workflow,'capacity',lambda mode:1)
    monkeypatch.setattr(workflow,'control',lambda *a,**k:calls.append(a))
    with db('live') as conn:conn.execute("UPDATE settings SET value='true' WHERE key='public_proxy_auth_approved'")
    workflow.tick();assert calls==[]
    with db('live') as conn:conn.execute("UPDATE batches SET status='completed' WHERE id='before'")
    workflow.tick();assert calls==[('live','after','resume')]


def test_rejected_coupon_formats_are_not_replayed_on_later_runs(client, monkeypatch):
    from protocol import har_tasks
    from protocol.taskflow.protocol_common import ActionRequired
    ctx=SimpleNamespace(mode='live',account_id=1,credentials={'scopes':{}},persist_scopes=lambda value:None,
                        ensure_allowed=lambda:None,transport=SimpleNamespace(observations=[]))
    monkeypatch.setattr(har_tasks,'VivoClient',lambda *a:None)
    monkeypatch.setattr(har_tasks,'BENEFITS',())
    monkeypatch.setattr(har_tasks,'COUPONS',(383,))
    writes=[]
    class Native:
        def __init__(self,*a):pass
        def shop_session(self):pass
        def call(self,host,path,**kwargs):
            if path=='/member/query':return {'level':1}
            if path=='/coupon/v2/queryCouponActivity':return {'couponModule':{},'couponModuleItems':[{'couponUuid':'coupon','receiveStatus':211201,'couponFlag':0}]}
            writes.append(kwargs)
            raise ActionRequired('参数错误' if 'form' in kwargs else '活动无效',provider_code='300' if 'form' in kwargs else '211415',reason_kind='provider_rejected')
    monkeypatch.setattr(har_tasks,'Native',Native)
    har_tasks.membership(SimpleNamespace(source_context=ctx))
    har_tasks.membership(SimpleNamespace(source_context=ctx))
    assert len(writes)==2


def test_panel_sync_preserves_renewed_vivo_id_with_stable_wechat_record(client, monkeypatch):
    import engine
    from adapter import import_accounts
    import_accounts('live',[{'openid':'wx:owner','_wx_openid':'owner','_pool':'https://panel.test'}])
    with db('live') as conn:
        conn.execute('UPDATE accounts SET credentials=?,status=\'active\'',(encrypt({'openid':'real-vivo-id','vivotoken':'owned-token','_wx_openid':'owner','_pool':'https://panel.test','saved_at':200}),))
    def events(*a,**k):
        yield {'type':'pool_start','total':1}
        yield {'type':'pool_accounts','pool_index':1,'items':[{'openid':'wx:owner','_wx_openid':'owner','_pool':'https://panel.test','panel_status':'online'}]}
        yield {'type':'pool_end'}
    monkeypatch.setattr(engine,'live_events',events)
    class ImmediateThread:
        def __init__(self,target,**k):self.target=target
        def start(self):self.target()
    monkeypatch.setattr(engine.threading,'Thread',ImmediateThread)
    engine.sync_pools('live')
    with db('live') as conn:
        rows=conn.execute('SELECT * FROM accounts').fetchall()
        assert len(rows)==1 and rows[0]['external_id']=='wx:owner'
        raw=decrypt(rows[0]['credentials'])
        assert raw['openid']=='real-vivo-id' and raw['vivotoken']=='owned-token'


def test_placeholder_import_cannot_overwrite_real_identity_at_equal_timestamp(client):
    from adapter import import_accounts
    import_accounts('live',[{'openid':'real-owner','vivotoken':'real-token','_wx_openid':'owner','saved_at':200}])
    import_accounts('live',[{'openid':'wx:owner','vivotoken':'placeholder-token','_wx_openid':'owner','saved_at':200}])
    with db('live') as conn:
        raw=decrypt(conn.execute('SELECT credentials FROM accounts').fetchone()[0])
        assert raw['openid']=='real-owner' and raw['vivotoken']=='real-token'


def test_placeholder_is_recovered_only_from_owned_official_session():
    from execution_policy import verified_identity
    raw={'openid':'wx:owner','_wx_openid':'owner','vivotoken':'same-token','unionid':'own-union'}
    fixed=verified_identity(raw,{'data':{'openId':'real-owner','unionId':'own-union'}})
    assert fixed['openid']=='real-owner' and fixed['vivotoken']=='same-token'
    assert raw['openid']=='wx:owner'
    with pytest.raises(ValueError):verified_identity(raw,{'data':{'openId':'another','unionId':'wrong-union'}})
    with pytest.raises(ValueError):verified_identity({**raw,'_wx_openid':'another'},{'data':{'openId':'real-owner'}})
    with pytest.raises(ValueError):verified_identity({'openid':'existing-owner'},{'data':{'openId':'someone-else'}})
