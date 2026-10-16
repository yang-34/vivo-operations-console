import json
import pytest
from adapter import import_accounts,import_legacy_ledger
from store import db,decrypt
from worker import parse_pool_urls
import engine


def test_panel_address_config_is_split_by_delimiters():
    assert parse_pool_urls('http://one:8000|http://two:9000;http://one:8000')==['http://one:8000','http://two:9000']
    assert parse_pool_urls(['http://one','http://one'])==['http://one']


def test_same_wechat_identity_deduplicates_and_old_file_cannot_replace_new_token(client):
    import_accounts('live',[{'openid':'vivo-a','vivotoken':'new-token','wx_openid':'wx-a','saved_at':200}])
    import_accounts('live',[{'openid':'wx:wx-a','_wx_openid':'wx-a','_pool':'http://panel','saved_at':100}])
    with db('live') as c:
        assert c.execute('SELECT count(*) FROM accounts').fetchone()[0]==1
        raw=decrypt(c.execute('SELECT credentials FROM accounts').fetchone()[0])
        assert raw['vivotoken']=='new-token' and raw['openid']=='vivo-a' and raw['_pool']=='http://panel'


def test_legacy_ledger_is_history_not_credentials_or_current_completion(client):
    import_accounts('live',[{'openid':'a','vivotoken':'t'}])
    import_legacy_ledger('live',{'date':'2026-10-13','done':{'a':{'label':'Old','gained':10,'ts':1},'b':{'label':'Missing','gained':4,'ts':2}}})
    with db('live') as c:
        assert c.execute('SELECT count(*) FROM accounts').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM legacy_history').fetchone()[0]==2
        assert c.execute('SELECT count(*) FROM ledger').fetchone()[0]==0
    assert client.get('/api/accounts',headers={'X-Data-Mode':'live'}).json['items'][0]['points'] is None


def test_public_proxy_auth_gate_sends_no_credentials(client,monkeypatch):
    import_accounts('live',[{'openid':'a','vivotoken':'secret'}])
    with db('live') as c:
        c.execute("UPDATE settings SET value='true' WHERE key='proxy_required'")
        account=dict(c.execute('SELECT * FROM accounts').fetchone())
    started=[]
    import protocol.runner
    monkeypatch.setattr(protocol.runner,'iter_events',lambda *a,**k:started.append(True))
    with pytest.raises(ValueError,match='等待'):
        list(engine.live_events('live',account,profile='sync'))
    assert not started
