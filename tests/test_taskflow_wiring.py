"""Standalone dispatch, persistence and optional native-device integration."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from protocol.api import Account, VivoApi
from protocol.runner import PLAN_VERSION, step_plan
from protocol.source_bridge import SOURCE_TASKS, run_groups, run_source, task_catalog
from protocol.taskflow.added_common import Journal
from protocol.taskflow.native_runtime import device_lock, mark_submission, settings
from protocol.taskflow.protocol_common import CURRENT, ActionRequired, ProtocolError
from protocol.taskflow.sdk import StepResult


def test_every_automatic_catalog_handler_is_in_plan_and_sdk_aliases_are_disabled():
    api = VivoApi(Account({'openid': 'offline', 'vivotoken': 'offline'}))
    try:
        plan = step_plan(api, 'full')
    finally:
        api.s.close()
    keys = [key for key, _, _ in plan]
    assert len(keys) == len(set(keys))
    assert not any(key.startswith(('sdk.', 'source.sdk_')) for key in keys)
    catalog = task_catalog()
    assert all(t['handler'] in set(keys) | {'balance.sync'} for t in catalog if t['default_enabled'])
    assert all(not t['default_enabled'] for t in catalog if t['key'].startswith('sdk_'))
    assert PLAN_VERSION > 5


def test_all_client_packages_and_new_real_actions_are_dispatched():
    source = dict(SOURCE_TASKS)
    assert len([key for key in source if key.startswith('client_tasks_')]) == 11
    assert {'browser_search', 'browser_video', 'browser_navigation', 'browser_notification',
            'browser_novel', 'shop_product_browse', 'shop_cart_add', 'published_business_signs'} <= source.keys()


def test_core_task_groups_use_journaled_source_handlers(monkeypatch):
    from protocol import runner
    calls = []
    monkeypatch.setattr(runner, 'run_groups', lambda api, keys: calls.extend(keys))
    api = VivoApi(Account({'openid': 'offline', 'vivotoken': 'offline'}))
    try:
        plan = {key: fn for key, _, fn in step_plan(api, 'full')}
        plan['tasks.center']()
        plan['tasks.ec']()
    finally:
        api.s.close()
    assert {'tasks_0', 'tasks_1', 'tasks_2', 'tasks_5', 'tasks_6', 'ec_browser', 'ec_vivospace'} <= set(calls)


def test_combined_groups_preserve_failure_and_continue_other_packages(monkeypatch):
    from protocol import source_bridge
    monkeypatch.setattr(source_bridge, 'run_source', lambda api, key: {'status': 'failed' if key == 'first' else 'completed'})
    result = run_groups(None, ['first', 'next'])
    assert [r['status'] for r in result['results']] == ['failed', 'completed']
    from protocol.runner import classify
    assert classify(result) == 'partial'


def test_native_configuration_is_optional_and_account_scoped(client, monkeypatch):
    import store
    monkeypatch.delenv('VIVO_NATIVE_DEVICES', raising=False)
    assert settings.VIVO_NATIVE_DEVICES == {}
    (store.DATA / 'native-devices.json').write_text(json.dumps({'1': {'serial': 'owned-device'}}), encoding='utf-8')
    assert settings.VIVO_NATIVE_DEVICES == {'1': {'serial': 'owned-device'}}
    monkeypatch.setenv('VIVO_NATIVE_DEVICES', '{"2":{"serial":"second-device"}}')
    assert '1' not in settings.VIVO_NATIVE_DEVICES
    monkeypatch.setenv('VIVO_NATIVE_DEVICES', '[]')
    with pytest.raises(ProtocolError):
        settings.VIVO_NATIVE_DEVICES


def test_native_device_lock_excludes_other_accounts_and_adb_paths(client):
    with device_lock('adb-one', 'same-device'):
        with pytest.raises(ActionRequired):
            with device_lock('adb-two', 'same-device'):
                pytest.fail('device was concurrently acquired')
        with device_lock('adb-two', 'another-device'):
            pass
    with device_lock('adb-two', 'same-device'):
        pass


def test_native_submission_sets_transport_write_marker():
    context = SimpleNamespace(transport=SimpleNamespace(unsafe_sent=False), ensure_allowed=Mock())
    token = CURRENT.set(context)
    try:
        mark_submission()
    finally:
        CURRENT.reset(token)
    assert context.transport.unsafe_sent
    context.ensure_allowed.assert_called_once()


def test_bundled_native_identity_scripts_exist():
    from protocol.taskflow.native_runtime import settings
    for name in ('vivo-native-identity.js', 'vivo-browser-identity.js', 'vivo-account-identity.js'):
        assert 'rpc.exports' in (settings.ROOT_DIR / 'scripts' / name).read_text(encoding='utf-8')


def test_bridge_keeps_diagnostics_from_continuations_and_final_native_skip(client, monkeypatch):
    from protocol.taskflow import vivo
    context = SimpleNamespace(credentials={'scopes': {}, 'device': {}}, ensure_allowed=lambda: None)
    api = SimpleNamespace(source_context=context, source_client=SimpleNamespace(http=SimpleNamespace(writing=False)), pm_points=lambda: 100)
    results = iter([
        StepResult('success', 'progress', {'points': 100}, continue_after=0,
                   diagnostics={'entries': [{'name': 'first', 'status': 'success', 'message': 'verified'}]}),
        StepResult('skipped', 'native missing', {'points': 100}, diagnostics={'skip_kind': 'native_unavailable'}),
    ])
    monkeypatch.setattr(vivo.LiveVivo, 'run', lambda *args: next(results))
    result = run_source(api, 'browser_search')
    assert len(result['results']) == 2
    assert result['results'][-1]['skip_kind'] == 'native_unavailable'


def test_native_pending_intent_survives_account_context_restart(client, monkeypatch):
    from protocol.taskflow.vivo_browser_tasks import execute_one
    from protocol.taskflow.compat import timezone
    def ctx():
        return SimpleNamespace(mode='live', account_id=1, credentials={'scopes': {}, 'session': {'openid': 'own'}}, persist_scopes=lambda scopes: None)
    first = ctx()
    key = f'{timezone.localdate()}:7:0'
    Journal(first, 'vivo_browser_real').put(key, {'state': 'pending'})
    driver = Mock()
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda context: driver)
    with pytest.raises(ActionRequired):
        execute_one(Mock(), Mock(), ctx(), {'id': 7, 'name': '搜索'}, 'search', {'points': 100}, lambda: [], 'ec_browser')
    driver.operate_task.assert_not_called()
    driver.close.assert_called_once()


def test_native_intent_is_recorded_once_before_first_input(client, monkeypatch):
    from protocol.taskflow.vivo_native_actions import NativeActions
    context = SimpleNamespace(mode='live', account_id=1, credentials={'scopes': {}},
        persist_scopes=lambda scopes: None, ensure_allowed=lambda: None,
        transport=SimpleNamespace(unsafe_sent=False))
    journal = Journal(context, 'native-input-test')
    driver = NativeActions(context)
    driver.config = {'adb_path': 'adb', 'serial': 'owned'}
    driver.before_input = Mock(side_effect=lambda: journal.put('input', {'state': 'pending'}))
    calls = []
    def run(command, **kwargs):
        if 'input' in command:
            assert journal.saved('input')['state'] == 'pending'
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b'ok')
    monkeypatch.setattr('protocol.taskflow.vivo_native_actions.subprocess.run', run)
    token = CURRENT.set(context)
    try:
        driver.adb('shell', 'pm', 'path', driver.package)
        assert not journal.saved('input')
        driver.adb('shell', 'input', 'tap', '1', '2')
        driver.adb('shell', 'input', 'tap', '3', '4')
    finally:
        CURRENT.reset(token)
    driver.before_input.assert_called_once()
    assert context.transport.unsafe_sent and len(calls) == 3


def test_browser_unavailable_content_does_not_leave_pending_intent(client, monkeypatch):
    from protocol.taskflow.vivo_browser_tasks import execute_one
    context = SimpleNamespace(mode='live', account_id=1, credentials={'scopes': {}}, persist_scopes=lambda scopes: None)
    driver = Mock()
    driver.operate_task.side_effect = ActionRequired('没有当前推送', reason_kind='unavailable')
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda ctx: driver)
    with pytest.raises(ActionRequired):
        execute_one(Mock(), Mock(), context, {'id': 8}, 'push_read', {'points': 100}, lambda: [], 'ec_browser')
    from store import db
    with db('live') as conn:
        assert conn.execute("SELECT count(*) FROM task_journals WHERE state='pending'").fetchone()[0] == 0
    driver.close.assert_called_once()


def test_browser_push_read_dispatch_and_music_use_bundled_identity():
    from protocol.taskflow.vivo_browser_tasks import kind_for
    from protocol.taskflow.vivo_native_actions import MusicActions
    assert kind_for({'name': '查看浏览器推送'}) == 'push_read'
    assert MusicActions.package == 'com.android.bbkmusic'
    assert (settings.ROOT_DIR / 'scripts' / MusicActions.identity_script).is_file()
