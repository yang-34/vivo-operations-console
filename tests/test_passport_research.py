import pytest
from test_app_sessions import payload
from protocol.app_session import parse_session
from protocol.passport import core_session,validate_params,user_show_params,sign,read_request,login_readiness


def test_core_cookie_format_uses_existing_identity_and_token_only():
    session=parse_session(payload())
    core=core_session(session)
    assert len(core['cookies'])==3
    values={r['name']:r['value'] for r in core['cookies']}
    assert values['vivo_account_cookie_iqoo_checksum']==values['vivo_account_cookie_iqoo_vivotoken']=='owned-token'
    assert core['openid']==session['openid'] and len(session['cookies'])==5


def test_core_cookie_conversion_does_not_extend_credential_expiry():
    session=parse_session(payload());session['cookies'][2]['expires_at']=2000000000
    core=core_session(session)
    assert all(cookie['expires_at']==2000000000 for cookie in core['cookies'])


def test_passport_signs_last_and_keeps_vivo_device():
    session=parse_session(payload());device={'model':'V2309A','android_ver':'14','sdk_int':34}
    for params in (validate_params(session,device),user_show_params(session,device)):
        assert params['model']=='V2309A' and params['openid']=='owned'
        assert params['s']==sign({k:v for k,v in params.items() if k!='s'})['s']
    sdk=user_show_params(session,device)
    assert sdk['imei']=='123456789012345' and sdk['oaid']==sdk['vaid']==sdk['aaid']==''


def test_research_tool_refuses_login_and_sms_endpoints():
    class NoNetwork:
        def post(self,*a,**k):pytest.fail('No request should be issued')
    for path in ('/usrlg/v5/fastLogin','/usrprd/v5/smsLogin/p1','/usrlg/v5/login/manual'):
        with pytest.raises(ValueError):read_request(NoNetwork(),path,{})


def test_old_or_foreign_random_num_does_not_count_as_relay_context():
    readiness=login_readiness({'randomNum':'mini-app-context','phone':'138****0000'},parse_session(payload()))
    assert readiness['sms_start_missing']==['phone']
    assert 'relay.randomNum' in readiness['fast_login_missing'] and 'relay_confirmation' in readiness['fast_login_missing']
