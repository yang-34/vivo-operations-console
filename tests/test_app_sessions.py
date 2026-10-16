import json,time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from app_sessions import import_session,load
from protocol.app_session import parse_session,account_data,configure_api
from store import db,decrypt


def payload(identity='owned',token='owned-token'):
    return {'cookies':[{'name':'vvc_openid','value':identity},{'name':'vivo_account_cookie_iqoo_openid','value':identity},
                       {'name':'vvc_r','value':token},{'name':'vivo_account_cookie_iqoo_vivotoken','value':token},
                       {'name':'vvc_signature','value':'original-signature'}],
            'user_agent':'VivoSpace/2.1 Mozilla/5.0 (Linux; Android 14; V2309A Build/UP1A.231005.007; wv) Chrome/124.0.0.0'}


def test_cookie_parser_preserves_har_cookie_array():
    raw=payload();har={'log':{'entries':[{'request':{'url':'https://pointstore.vivo.com.cn/api/mall/item/exchange.do',
        'cookies':raw['cookies'],'headers':[{'name':'Cookie','value':'vvc_elapsedtime=15'},{'name':'User-Agent','value':raw['user_agent']}]}}]}}
    session=parse_session(har)
    assert len(session['cookies'])==6 and session['openid']=='owned'
    assert session['user_agent']==raw['user_agent']
    assert parse_session('vvc_openid=owned; vvc_r=owned-token')['openid']=='owned'


def test_parser_rejects_cross_account_expired_header_injection_and_foreign_domain():
    bad=payload();bad['cookies'][1]['value']='another'
    with pytest.raises(ValueError):parse_session(bad)
    bad=payload();bad['cookies'][2]['expires']=time.time()-1
    with pytest.raises(ValueError):parse_session(bad)
    with pytest.raises(ValueError):parse_session('vvc_openid=owned; vvc_r=token\r\nx-other:secret')
    bad=payload();bad['cookies'][0]['domain']='example.org'
    with pytest.raises(ValueError):parse_session(bad)


def test_import_is_encrypted_and_binds_only_matching_account(client):
    from adapter import import_accounts
    import_accounts('live',[{'openid':'owned','vivotoken':'mini-token'}])
    imported=import_session('live',{'input':payload(),'account_id':1})
    assert imported['status']=='unverified'
    with db() as c:
        row=c.execute('SELECT * FROM app_sessions').fetchone()
        raw=decrypt(c.execute('SELECT credentials FROM accounts WHERE id=1').fetchone()[0])
        assert 'owned-token' not in row['payload'] and raw['vivotoken']=='mini-token'
    with pytest.raises(ValueError):import_session('live',{'input':payload('another'),'account_id':1})
    with pytest.raises(ValueError):load('live',1)
    result=client.get('/api/accounts/1/app-session',headers={'X-Data-Mode':'live'})
    assert result.json['cookie_count']==5 and 'owned-token' not in result.text


def test_read_only_verification_then_exchange_uses_original_app_session(client,monkeypatch):
    from protocol import runner
    from protocol.api import Account,VivoApi
    from protocol.market import mall_call
    import app_sessions
    imported=import_session('live',{'input':payload(),'create_account':True})
    aid=imported['account_id'];assert aid==1
    def events(*args,**kwargs):
        assert kwargs['action']=='app_session_check'
        yield {'type':'app_session','points':250,'nickname':'Original app account'}
    monkeypatch.setattr('engine.live_events',events)
    monkeypatch.setattr('proxies.lease',lambda *a:nullcontext(None))
    response=client.post('/api/accounts/1/app-session/verify',headers={'X-Data-Mode':'live'},json={})
    assert response.status_code==200 and response.json['points']==250
    session,_=load('live',aid)
    with db() as c:raw=decrypt(c.execute('SELECT credentials FROM accounts').fetchone()[0])
    api=VivoApi(Account(account_data(session,raw)));configure_api(api,session)
    assert api._pm_headers()['Cookie'].count('vvc_signature=original-signature')==1
    calls=[]
    api.s.close();api.s=SimpleNamespace(request=lambda *a,**kw:(calls.append(kw) or SimpleNamespace(json=lambda:{'retcode':1034})))
    result=mall_call(api,{'redemption_kind':'standard'},'/api/mall/item/exchange.do',{'itemId':1},'POST')
    assert result['retcode']==1034 and len(calls)==1
    assert calls[0]['headers']['User-Agent']==session['user_agent']
    assert 'vvc_signature=original-signature' in calls[0]['headers']['cookie']
    assert 'vivo_member_source_type=APP_H5' not in calls[0]['headers']['cookie'] # no synthetic additions


def test_unverified_app_session_stops_before_exchange(client):
    from protocol.runner import iter_events
    import_session('live',{'input':payload(),'create_account':True})
    with db() as c:raw=decrypt(c.execute('SELECT credentials FROM accounts').fetchone()[0])
    result=list(iter_events({'mode':'live','account_id':1,'account':raw,'action':'exchange','product':{},'expected_price':5}))
    assert result[-1]['type']=='error' and not any(r['type']=='exchange_submit' for r in result)


def test_session_check_uses_only_app_read_endpoints(client,monkeypatch):
    from protocol import runner
    from protocol.api import Account,VivoApi
    from protocol.transport import Transport
    import_session('live',{'input':payload(),'create_account':True})
    with db() as c:raw=decrypt(c.execute('SELECT credentials FROM accounts').fetchone()[0])
    def create(raw,transport):
        api=VivoApi(Account(raw))
        api.user_info=lambda:pytest.fail('Do not authenticate an App token through the mini-app API')
        api._pm_get=lambda path: {'retcode':0,'login':True,'data':{'points':250,'userName':'Owned'}}
        return api
    monkeypatch.setattr(runner,'make_api',create)
    result=list(runner.iter_events({'mode':'live','account_id':1,'account':raw,'action':'app_session_check'}))
    assert len(result)==1 and result[0]['type']=='app_session'
    assert result[0]['points']==250 and result[0]['nickname']=='Owned' and result[0]['session_version']


def test_app_credentials_do_not_leak_when_transport_is_reused(client):
    from protocol.runner import make_api,transport_for
    transport=transport_for({'mode':'live'})
    session=parse_session(payload())
    first=make_api(account_data(session,{}),transport)
    configure_api(first,session)
    assert 'original-signature' in first.cookie_header()
    transport=transport_for({'mode':'live'})
    second=make_api({'openid':'second-account','vivotoken':'second-token'},transport)
    assert 'original-signature' not in second.cookie_header()
    assert 'owned-token' not in second.cookie_header()
    assert second.s.cookies.get('vvc_openid')=='second-account'
