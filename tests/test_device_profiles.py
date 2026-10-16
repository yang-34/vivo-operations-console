import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from protocol.api import Account, VivoApi
from protocol.common import gen_imei, gen_oaid
from protocol.devices import MODEL_CATALOG, ensure_device_profile, resolve_device, webview_ua


def test_profiles_cover_vivo_catalog_and_keep_identifiers(monkeypatch):
    for entry in MODEL_CATALOG:
        monkeypatch.setattr('protocol.devices.secrets.choice', lambda values: entry if values is MODEL_CATALOG else values[0])
        raw={'openid':'legacy-account','vivotoken':'secret'}
        saved=ensure_device_profile(raw,preserve_legacy=True)
        d=resolve_device(saved)
        assert (d['model'],d['android_ver'],d['screensize'])==(entry[0],entry[2],entry[3])
        assert d['sdk_int']==int(d['android_ver'])+20
        assert d['imei']==gen_imei(raw['openid']) and d['oaid']==gen_oaid(raw['openid'])
        assert d['profile_source']=='assigned-vivo' and not d['native_verified']
        assert 'protocol_device' not in raw
        assert ensure_device_profile(saved) is saved
        assert resolve_device(json.loads(json.dumps(saved)))==d


def test_new_accounts_have_distinct_random_identifiers_and_keep_existing_ids():
    profiles=[resolve_device(ensure_device_profile({'openid':'new-account'})) for _ in range(20)]
    assert len({d['imei'] for d in profiles})==20
    assert len({d['oaid'] for d in profiles})==20
    raw={'openid':'owned','imei':'owned-imei','device':{'oaid':'owned-oaid'}}
    d=resolve_device(ensure_device_profile(raw))
    assert d['imei']=='owned-imei' and d['oaid']=='owned-oaid'


def test_original_devices_are_preserved_but_generated_data_is_not_native():
    raw={'device':{'brand':'vivo','model':'V2309A','android_ver':'14','sdk_int':34,'imei':'original'}}
    assert ensure_device_profile(raw) is raw
    assert resolve_device(raw)['native_verified']
    generated=resolve_device(ensure_device_profile({'openid':'new'}))
    assert not resolve_device({'device':generated})['native_verified']
    conflicting={'device':{'brand':'Xiaomi','model':'V2309A','android_ver':'14','sdk_int':34}}
    assert not resolve_device(conflicting)['native_verified']


def test_headers_cookies_sdk_and_source_share_profile(client,monkeypatch):
    from protocol.source_bridge import Context,CURRENT
    from protocol.taskflow.vivo import VivoClient
    from protocol.taskflow.vendor.vivo import jvq
    from protocol.market import mall_call
    monkeypatch.setattr('protocol.devices.secrets.choice',lambda values: MODEL_CATALOG[-1] if values is MODEL_CATALOG else values[0])
    raw=ensure_device_profile({'openid':'owned','vivotoken':'owned-token'})
    api=VivoApi(Account(raw))
    d=api.acct.device
    assert d['model']=='V2366GA'
    assert api._h5_headers()['User-Agent']==webview_ua(d)
    assert d['model'] in api._pm_headers()['User-Agent'] and d['model'] in api.ua_wap
    assert api.s.cookies.get('vvc_imei')==d['imei']
    assert d['imei'] in api.cookie_header()
    assert api._base_params()['model']==d['model']
    context=Context(api,'live',0,raw)
    assert context.credentials['device']==d
    current=CURRENT.set(context)
    try:
        source=VivoClient(context.credentials)
    finally:
        CURRENT.reset(current)
    assert source.ua==api.ua_h5
    requests=[]
    class Session:
        def post(self,url,**kw):
            requests.append(json.loads(jvq.decrypt_response(kw['json']['jvq_param'])))
            return SimpleNamespace(json=lambda:{'code':0,'data':{}})
        def request(self,method,url,**kw):
            requests.append(kw)
            return SimpleNamespace(json=lambda:{'retcode':0,'data':{}})
    api.s.close();api.s=Session()
    api._sdk_upload('com.vivo.space','sample',1)
    assert requests[-1]['model']==d['model'] and str(requests[-1]['av'])=='14'
    assert str(requests[-1]['adrVerName'])=='34'
    mall_call(api,{},'/api/mall/item/detail.do',{'id':1})
    assert d['model'] in requests[-1]['headers']['User-Agent']
    assert 'vvc_model='+d['model'] in requests[-1]['headers']['cookie']


def test_import_merge_and_concurrent_import_preserve_profile(client):
    from adapter import import_accounts
    from store import db,decrypt
    raw={'openid':'import-stable','vivotoken':'token1'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:import_accounts('live',[raw]),range(2)))
    with db() as c:
        rows=c.execute('SELECT * FROM accounts WHERE external_id=?',(raw['openid'],)).fetchall()
    assert len(rows)==1
    before=decrypt(rows[0]['credentials'])['protocol_device']
    import_accounts('live',[{**raw,'vivotoken':'token2','protocol_device':{'model':'unwanted-reset'}}])
    with db() as c:after=decrypt(c.execute('SELECT credentials FROM accounts WHERE external_id=?',(raw['openid'],)).fetchone()[0])
    assert after['protocol_device']==before and after['vivotoken']=='token2'
    public=client.get('/api/accounts/'+str(rows[0]['id']),headers={'X-Data-Mode':'live'}).json['account']['device']
    assert public['model']==before['model'] and public['profile_source']=='assigned-vivo'
    assert 'imei' not in public and 'oaid' not in public


def test_migration_is_idempotent_and_preserves_account_data(client):
    from store import db,encrypt,decrypt,now,migrate_device_profiles
    original={'openid':'migrate-old','vivotoken':'keep-token','session':{'private':'keep'},'saved_at':123}
    with db() as c:
        c.execute('INSERT INTO accounts(external_id,nickname,credentials,created_at,points,status) VALUES(?,?,?,?,?,?)',
                  ('migrate-old','keep-name',encrypt(original),now(),123,'disabled'))
    assert migrate_device_profiles()==1
    with db() as c:first=dict(c.execute("SELECT * FROM accounts WHERE external_id='migrate-old'").fetchone())
    migrated=decrypt(first['credentials'])
    assert {k:v for k,v in migrated.items() if k!='protocol_device'}==original
    assert migrated['protocol_device']['imei']==gen_imei(original['openid'])
    assert first['status']=='disabled' and first['points']==123 and first['nickname']=='keep-name'
    assert migrate_device_profiles()==0
    with db() as c:second=dict(c.execute("SELECT * FROM accounts WHERE external_id='migrate-old'").fetchone())
    assert second==first
