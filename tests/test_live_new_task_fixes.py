import json
from types import SimpleNamespace

import requests


def test_sdk_cache_preserves_real_event_rules_without_account_progress(monkeypatch):
    from protocol.transport import Transport
    cached = {'sdk-actions:com.vivo.space': {'code': 0, 'data': {'actions': [{'actionId': 'stale'}]}}}
    calls = []
    scene = [{'eventId': '081|001|55|077', 'condition': [], 'logic': ''}]
    def request(*args, **kwargs):
        calls.append(1)
        response = requests.Response(); response.status_code = 200
        response._content = json.dumps({'code': 0, 'data': {'actions': [
            {'actionId': 'current', 'scene': scene, 'userProgress': 12, 'unreceviedPoints': 5}
        ]}}).encode()
        return response
    monkeypatch.setattr(requests.Session, 'request', request)
    transport = Transport(gap=0)
    transport.cache_get = cached.get
    transport.cache_put = lambda key, value: cached.update({key: value})
    url = 'https://pointsdk.vivo.com.cn/sdk/config/action/list'
    first = transport.post(url, params={'pkgName': 'com.vivo.space'}).json()
    second = transport.post(url, params={'pkgName': 'com.vivo.space'}).json()
    assert len(calls) == 1 and first['data']['actions'][0]['actionId'] == 'current'
    action = second['data']['actions'][0]
    assert action['scene'] == scene
    assert 'userProgress' not in action and 'unreceviedPoints' not in action
    transport.close()


def test_targeted_sample_does_not_skip_or_mark_the_whole_plan_complete(client, monkeypatch):
    import engine
    from adapter import import_accounts
    from store import db, now, today
    from protocol.runner import PLAN_VERSION
    import_accounts('live', [{'openid': 'sample-account', 'vivotoken': 'test'}])
    with db('live') as conn:
        aid = conn.execute('SELECT id FROM accounts').fetchone()[0]
        conn.execute("INSERT INTO batches VALUES('sample','sample','full','running',1,1,?,NULL)", (now(),))
        rid = conn.execute("INSERT INTO runs(batch_id,account_id,status) VALUES('sample',?,'running')", (aid,)).lastrowid
    observed = []
    def events(mode, account, **kwargs):
        observed.append(kwargs.get('only_steps'))
        yield {'type': 'balance', 'points': 100}
        yield {'type': 'step', 'step_key': 'source.shop_product_browse', 'label': 'browse', 'status': 'completed'}
        yield {'type': 'result', 'before': 100, 'points': 105, 'gained': 5}
    monkeypatch.setattr(engine, 'live_events', events)
    engine.run_one('live', rid, 'full', only_steps=['source.shop_product_browse'])
    assert observed == [['source.shop_product_browse']]
    with db('live') as conn:
        assert conn.execute('SELECT count(*) FROM ledger').fetchone()[0] == 0
        assert not conn.execute('SELECT 1 FROM task_checkpoints WHERE account_id=? AND day=? AND step_key=?', (aid, today(), f'plan:v{PLAN_VERSION}:full')).fetchone()
        run = conn.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        assert run['status'] == 'completed' and run['gained'] == 5
