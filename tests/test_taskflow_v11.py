"""TaskFlow vivo/11 regressions adapted to the standalone SQLite runtime."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from protocol.taskflow.compat import timezone
from protocol.taskflow.added_common import Journal
from protocol.taskflow.protocol_common import ActionRequired, ProtocolError
from protocol.taskflow.sdk import Identity, StepResult, UnknownWriteResult
from protocol.taskflow.vivo import LiveVivo
from protocol.taskflow.vivo_business_signs import execute as signs
from protocol.taskflow.vivo_client_tasks import catalog, execute
from protocol.taskflow.vivo_appointments import discover
from protocol.taskflow.vivo_browser_tasks import BrowserActions, execute_one, kind_for, playback
from protocol.taskflow.vivo_shop_actions import browse_events, detail, perform

@pytest.fixture(autouse=True)
def isolated_store(client):
    pass

def context():
    ctx = SimpleNamespace(mode='live', account_id=1,
        credentials={'session': {'openid': 'own', 'vivotoken': 'token'}, 'scopes': {}},
        checkpoint={'points': 100}, ensure_allowed=lambda: None, persist_scopes=lambda scopes: None)
    ctx.persist_scopes = lambda scopes: ctx.credentials.update(scopes=scopes)
    return ctx

def test_package_catalog_uses_source_post_and_keeps_unfamiliar_categories():
    c = Mock(pkg='original')
    one = {'id': 1, 'name': '新类型实际任务'}
    two = {'id': 2, 'name': '另一个任务'}
    c._call.return_value = {'data': {'tasks': [{'type': 3, 'list': [one]}, {'type': 9, 'list': [two]}]}}
    assert catalog(c, 'browser') == [one, two]
    assert c._call.call_args.kwargs['method'] == 'POST'
    assert c._call.call_args.kwargs['pkg'] == 'com.vivo.browser'
    assert c._call.call_args.kwargs['writes'] is False

def test_catalog_expands_only_published_has_more_group():
    c = Mock()
    c._call.return_value = {'data': {'tasks': [{'type': 6, 'hasMore': True, 'list': [{'id': 1}]}]}}
    c.group_list.return_value = [{'id': 1}, {'id': 2}]
    assert len(catalog(c, 'iTheme')) == 2
    c.group_list.assert_called_once_with('6')

def test_matrix_executes_web_task_then_verifies_progress_and_real_reward():
    ctx = context()
    t = {'id': 1, 'name': '浏览官方页面', 'webTask': 1, 'keyId': 'official', 'jumpURL': 'https://zhan.vivo.com.cn/published', 'completedTimes': 0, 'totalTime': 1, 'unreceviedPoints': 0}
    state = deepcopy(t)
    c = Mock(pkg='original')
    c._call.side_effect = lambda *args, **kwargs: {'data': {'tasks': [{'type': 3, 'list': [deepcopy(state)]}]}}

    def complete(key):
        state.update(completedTimes=1, taskComplete=True, unreceviedPoints=5)

    def reward(task):
        state['unreceviedPoints'] = 0
        return {'data': {'rewardPoints': 5}}
    c.complete_web_task.side_effect = complete
    c.receive_point.side_effect = reward
    adapter = LiveVivo()
    adapter.profile = Mock(return_value=Identity('owned', 'masked', {'points': 105}))
    result = execute(adapter, c, ctx, 'client_tasks_browser', ctx.checkpoint)
    assert result.status == 'success' and result.snapshot['points'] == 105
    c.visit.assert_called_once_with(t['jumpURL'])
    c.complete_web_task.assert_called_once_with('official')
    c.receive_point.assert_called_once()
    assert c.pkg == 'original'

def test_purchase_flagged_web_task_is_not_completed_by_web_callback():
    ctx = context()
    task = {'id': 1, 'name': '购买会员领积分', 'webTask': True, 'keyId': 'official', 'jumpURL': 'https://zhan.vivo.com.cn/page', 'completedTimes': 0, 'totalTime': 1}
    c = Mock(pkg='original')
    c._call.return_value = {'data': {'tasks': [{'list': [task]}]}}
    result = execute(LiveVivo(), c, ctx, 'client_tasks_browser', ctx.checkpoint)
    assert result.status == 'action_required'
    c.visit.assert_not_called()
    c.complete_web_task.assert_not_called()
    c.receive_point.assert_not_called()

def test_retired_wallet_sign_uses_current_native_replacement(monkeypatch):
    from protocol.taskflow.protocol_common import ActionRequired
    ctx = context()
    c = Mock()
    c._call.side_effect = [{'data': [{'bizCode': 'walletWelfare', 'packageName': 'com.vivo.wallet', 'isSign': 0}]}, ActionRequired('retired', provider_code='20041')]
    replacement = Mock(return_value=StepResult('skipped', 'native already signed', ctx.checkpoint))
    monkeypatch.setattr('protocol.taskflow.vivo_native_updates.execute', replacement)
    result = signs(Mock(), c, ctx, 'published_business_signs', ctx.checkpoint)
    assert result.status == 'skipped'
    assert replacement.call_args.args[3] == 'wallet_native_sign'

def test_new_task_link_feeds_activity_discovery_without_erasing_write_intents():
    from protocol.taskflow.vivo_client_tasks import remember_sites
    ctx = context()
    ctx.credentials['scopes'] = {'_taskflow_vivo_appointment_catalog': {'version': 2}, '_taskflow_vivo_appointment_sign': {'original': {'state': 'pending'}}}
    remember_sites(ctx, 'vivospace', [{'jumpURL': 'https://zhan.vivo.com.cn/vivospace/wk261008324ff1c9'}])
    scope = ctx.credentials['scopes']
    assert scope['_taskflow_vivo_client_sites']['packages']['vivospace'] == ['wk261008324ff1c9']
    assert '_taskflow_vivo_appointment_catalog' not in scope
    assert scope['_taskflow_vivo_appointment_sign']['original']['state'] == 'pending'

def test_real_behavior_handler_runs_before_primary_catalog_readback(monkeypatch):
    ctx = context()
    c = Mock(pkg='original')
    task = {'id': 1, 'name': '阅读3篇帖子', 'completedTimes': 0, 'totalTime': 1}
    c._call.return_value = {'data': {'tasks': [{'list': [task]}]}}
    behavior = Mock(return_value=StepResult('success', 'real read', {'points': 105}, diagnostics={'entries': [{'name': 'actual community read', 'status': 'success'}]}))
    monkeypatch.setattr('protocol.taskflow.vivo_native_earn.execute', behavior)
    adapter = Mock()
    adapter.task_group.return_value = StepResult('skipped', 'readback', {'points': 105}, diagnostics={'entries': []})
    result = execute(adapter, c, ctx, 'client_tasks_vivospace', ctx.checkpoint)
    behavior.assert_called_once()
    assert result.snapshot['points'] == 105 and c.pkg == 'original'

def test_pending_legacy_task_does_not_replay_and_other_items_execute():
    ctx = context()
    Journal(ctx, 'vivo_tasks').put('ec_browser:reward:1', {'state': 'pending'})
    c = Mock(pkg='original')
    c._call.return_value = {'data': {'tasks': [{'list': [{'id': 1, 'name': 'pending'}, {'id': 2, 'name': 'next'}]}]}}
    a = Mock()
    a.task_group.return_value = StepResult('skipped', 'done', ctx.checkpoint, diagnostics={'entries': []})
    result = execute(a, c, ctx, 'client_tasks_browser', ctx.checkpoint)
    a.task_group.assert_called_once()
    assert result.diagnostics['entries'][0]['reason_kind'] == 'settlement'
    assert Journal(ctx, 'vivo_tasks').saved('ec_browser:reward:1')['state'] == 'pending'

def test_unknown_item_does_not_block_next_item_or_report_income():
    ctx = context()
    c = Mock(pkg='original')
    c._call.return_value = {'data': {'tasks': [{'list': [{'id': 1}, {'id': 2}]}]}}
    a = Mock()
    a.task_group.side_effect = [UnknownWriteResult(), StepResult('skipped', 'done', ctx.checkpoint, diagnostics={'entries': []})]
    result = execute(a, c, ctx, 'client_tasks_browser', ctx.checkpoint)
    assert a.task_group.call_count == 2 and result.status == 'action_required'
    assert result.snapshot['points'] == 100 and c.pkg == 'original'

def test_manual_items_do_not_create_endless_four_item_continuation():
    ctx = context()
    c = Mock(pkg='original')
    c._call.return_value = {'data': {'tasks': [{'list': [{'id': i} for i in range(8)]}]}}
    a = Mock()
    a.task_group.return_value = StepResult('action_required', 'native unavailable', ctx.checkpoint, diagnostics={'entries': []})
    result = execute(a, c, ctx, 'client_tasks_browser', ctx.checkpoint)
    assert a.task_group.call_count == 8 and result.continue_after is None

def test_published_wallet_welfare_sign_uses_server_code_and_package():
    ctx = context()
    signed = False
    c = Mock()

    def call(host, path, **kwargs):
        nonlocal signed
        if path.endswith('signBizList.do'):
            return {'data': [{'bizCode': 'walletWelfare', 'packageName': 'com.vivo.wallet', 'bizName': '钱包福利', 'isSign': 0}]}
        if path.endswith('info.do'):
            return {'data': {'todayIsSign': signed}}
        assert kwargs['params'] == {'sysCode': 'walletWelfare'}
        assert kwargs['pkg'] == 'com.vivo.wallet'
        signed = True
        return {'data': {'awards': [{'awardNum': 1}]}}
    c._call.side_effect = call
    a = SimpleNamespace(profile=lambda client: Identity('owned', 'masked', {'points': 101}))
    result = signs(a, c, ctx, 'published_business_signs', ctx.checkpoint)
    assert result.status == 'success' and signed
    assert result.snapshot['points'] == 101

def test_pending_legacy_sign_cannot_be_replayed_through_published_signs():
    ctx = context()
    Journal(ctx, 'vivo_sign').put('sign_wallet', {'state': 'pending'})
    c = Mock()
    c._call.side_effect = [{'data': [{'bizCode': 'walletWelfare', 'packageName': 'com.vivo.wallet', 'isSign': 0}]}, {'data': {'todayIsSign': False}}]
    result = signs(Mock(), c, ctx, 'published_business_signs', ctx.checkpoint)
    assert c._call.call_count == 2 and result.status == 'action_required'

@pytest.mark.parametrize('key', ['client_tasks_browser', 'client_tasks_iTheme', 'published_business_signs'])
def test_added_workflows_registered_as_real_execution(key):
    spec = next((t for t in LiveVivo.tasks if t.key == key))
    assert spec.writes and spec.automatic

@pytest.mark.parametrize('name,kind', [('完成5次搜索', 'search'), ('观看5分钟视频', 'video'), ('点击3个顶部导航站点', 'navigation'), ('打开浏览器通知开关', 'notification'), ('阅读1本小说', 'novel'), ('贷款领积分', None)])
def test_browser_tasks_have_concrete_action_dispatch(name, kind):
    assert kind_for({'name': name}) == kind

def test_video_requires_owned_actively_playing_session():
    raw = 'Session #0:\n package=com.vivo.browser\n state=PlaybackState {state=3, position=2000, buffered position=5000, speed=1.0}\n'
    assert playback(raw, 'com.vivo.browser') == 2000
    assert playback(raw.replace('state=3', 'state=2'), 'com.vivo.browser') is None
    assert playback(raw.replace('com.vivo.browser', 'other.player'), 'com.vivo.browser') is None
    assert playback(raw + ' package=other.player', 'com.vivo.browser') is None

def test_real_video_does_not_turn_a_paused_player_into_watch_time(monkeypatch):
    d = BrowserActions(context())
    d.click = Mock()
    d.foreground = Mock()
    d.adb = Mock(return_value='package=com.vivo.browser state=PlaybackState {state=2, position=2000, speed=0.0}')
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.time.sleep', Mock())
    with pytest.raises(ActionRequired):
        d.video()
    assert all(('upload' not in str(call) for call in d.adb.call_args_list))

def test_unavailable_native_account_is_skipped_before_any_intent(monkeypatch):
    ctx = context()
    driver = Mock()
    driver.setup.side_effect = ActionRequired('unavailable', reason_kind='session_unavailable')
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda _: driver)
    result = execute_one(Mock(), Mock(), ctx, {'id': 4, 'name': '搜索'}, 'search', ctx.checkpoint, Mock(), 'ec_browser')
    assert result.status == 'skipped' and result.diagnostics['skip_kind'] == 'native_unavailable'
    assert not any(ctx.credentials['scopes'].values())
    driver.operate_task.assert_not_called()
    driver.close.assert_called_once()

def test_native_identity_mismatch_prevents_ui_input():
    d = BrowserActions(context())
    d.script = Mock()
    d.script.exports_sync.identity.return_value = {'openid': 'another-account'}
    d.adb = Mock()
    with pytest.raises(ActionRequired):
        d.foreground()
    d.adb.assert_not_called()

def test_real_search_without_official_progress_is_not_a_reward(monkeypatch):
    ctx = context()
    driver = Mock()
    driver.operate_task.return_value = {'physical_search_submitted': True}
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda _: driver)
    task = {'id': 4, 'name': '搜索', 'completedTimes': 0, 'totalTime': 1}
    client, adapter = (Mock(), Mock())
    result = execute_one(adapter, client, ctx, task, 'search', ctx.checkpoint, lambda: [task], 'ec_browser')
    assert result.status == 'action_required' and result.snapshot['points'] == 100
    assert result.continue_after == 1
    client._call.assert_not_called()
    adapter.task_group.assert_not_called()

def test_native_progress_is_rechecked_before_claiming(monkeypatch):
    ctx = context()
    driver = Mock()
    driver.operate_task.return_value = {'physical_search_submitted': True}
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda _: driver)
    task = {'id': 4, 'name': '搜索', 'completedTimes': 0, 'totalTime': 1}
    adapter = Mock()
    adapter.task_group.return_value = StepResult('success', 'claimed', {'points': 105})
    result = execute_one(adapter, Mock(), ctx, task, 'search', ctx.checkpoint, lambda: [{**task, 'unreceviedPoints': 5}], 'ec_browser')
    assert result.snapshot['points'] == 105
    adapter.task_group.assert_called_once()

def test_product_event_is_only_exact_current_unconditional_scene():
    c = Mock()
    c._call.return_value = {'data': {'actions': [{'actionId': 'real', 'scene': [{'eventId': '081|001|55|077', 'condition': [], 'logic': ''}]}, {'actionId': 'conditional', 'scene': [{'eventId': '081|001|55|077', 'condition': [{'field': 'purchase'}]}]}, {'actionId': 'other', 'scene': [{'eventId': 'search', 'condition': []}]}]}}
    assert browse_events(c) == ['real']

def test_incomplete_product_identity_never_authorizes_event():
    n = Mock()
    n.call.side_effect = [{'commoditySpu': {}}, {'commoditySkuBaseInfo': {'skuId': 'wrong'}}]
    with pytest.raises(ProtocolError):
        detail(n, 'spu', 'sku')

def test_cart_reads_actual_owned_cart_and_never_submits_an_order(monkeypatch):
    n = Mock()
    n.call.side_effect = ['csrf', [], {'cartId': 'owned-cart'}, [{'mainSku': {'skuId': '22'}}]]
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.Native', lambda _: n)
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.products', lambda _: [('11', '22')])
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.detail', lambda *args: {'product_detail_sha256': 'digest'})
    ctx = context()
    c = Mock()
    result = perform(c, ctx, {'id': 15}, 'shop_cart_add', Journal(ctx, 'vivo_shop_actions'), 'cart')
    assert result['cart_sku_read_back'] is True and result['order_submitted'] is False
    writes = [call for call in n.call.call_args_list if call.kwargs.get('writes')]
    assert len(writes) == 1 and writes[0].args[1].endswith('/shopcart/addCommodity')
    assert writes[0].kwargs['body']['num'] == 1

def test_existing_cart_product_is_not_duplicated(monkeypatch):
    n = Mock()
    n.call.side_effect = ['csrf', [{'skuId': '22'}]]
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.Native', lambda _: n)
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.products', lambda _: [('11', '22')])
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.detail', lambda *args: {})
    ctx = context()
    with pytest.raises(ActionRequired):
        perform(Mock(), ctx, {'id': 15}, 'shop_cart_add', Journal(ctx, 'vivo_shop_actions'), 'cart')
    assert not any(ctx.credentials['scopes'].values())
    assert not any((c.kwargs.get('writes') for c in n.call.call_args_list))

def test_unknown_cart_submission_cannot_be_replayed(monkeypatch):
    n = Mock()
    n.call.side_effect = ['csrf', [], UnknownWriteResult()]
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.Native', lambda _: n)
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.products', lambda _: [('11', '22')])
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.detail', lambda *args: {})
    ctx = context()
    j = Journal(ctx, 'vivo_shop_actions')
    with pytest.raises(UnknownWriteResult):
        perform(Mock(), ctx, {'id': 15}, 'shop_cart_add', j, 'cart')
    assert j.saved('cart')['state'] == 'pending'
    n.call.side_effect = ['csrf', []]
    with pytest.raises(ActionRequired):
        perform(Mock(), ctx, {'id': 15}, 'shop_cart_add', j, 'cart')
    assert len([c for c in n.call.call_args_list if c.kwargs.get('writes')]) == 1

def test_composed_live_page_discovers_child_config_without_reusing_parent_ids(monkeypatch):
    ctx = context()
    monkeypatch.setattr('protocol.taskflow.vivo_appointments.sites', lambda _: ['wk2609303c903cd9'])
    monkeypatch.setattr('protocol.taskflow.vivo_appointments.SEEDS', set())
    monkeypatch.setattr('protocol.taskflow.vivo_appointments.site_config', lambda api, site: {'link': 'https://mshopact.vivo.com.cn/vivospace/wk260923cff5d7dd'} if site == 'wk2609303c903cd9' else {})
    client = Mock()
    client.http.hosts = set()
    first = discover(client, ctx)
    assert first['todo'] == ['wk260923cff5d7dd']
    assert first['targets'] == []
    second = discover(client, ctx)
    assert second['todo'] == [] and len(second['visited']) == 2
    assert not ctx.credentials['scopes'].get('_taskflow_vivo_appointment_sign')

def test_product_read_precedes_upload_and_only_server_ready_reward_is_claimed(monkeypatch):
    n = Mock()
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.Native', lambda _: n)
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.products', lambda _: [('11', '22')])
    order = []
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.detail', lambda *args: order.append('real_detail') or {'product_detail_sha256': 'digest'})
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.browse_events', lambda _: ['current_action'])
    c = Mock()

    def call(host, path, **kwargs):
        assert order[0] == 'real_detail'
        order.append(path)
        if path.endswith('/upload'):
            assert kwargs['params']['count'] == '1'
            return {'data': {'taskId': 'current_task', 'sdkTaskNotifyVo': {'actionId': 'current_action', 'taskId': 'current_task', 'points': 5, 'syncReceive': 0}}}
        assert path == '/sdk/task/receive/point'
        return {'data': {}}
    c._call.side_effect = call
    ctx = context()
    c.session = ctx.credentials['session']
    evidence = perform(c, ctx, {'id': 24}, 'shop_product_browse', Journal(ctx, 'vivo_shop_actions'), 'actual_detail', read_points=Mock(side_effect=[100, 105]))
    assert evidence['sdk_reward_points'] == 5
    assert order == ['real_detail', '/sdk/action/upload', '/sdk/task/receive/point']

def test_product_upload_without_reward_notice_does_not_probe_awards(monkeypatch):
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.Native', lambda _: Mock())
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.products', lambda _: [('11', '22')])
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.detail', lambda *args: {})
    monkeypatch.setattr('protocol.taskflow.vivo_shop_actions.browse_events', lambda _: ['current_action'])
    c = Mock()
    c._call.return_value = {'data': {'taskId': 'task'}}
    ctx = context()
    c.session = ctx.credentials['session']
    read = Mock()
    evidence = perform(c, ctx, {'id': 24}, 'shop_product_browse', Journal(ctx, 'vivo_shop_actions'), 'actual_detail', read_points=read)
    assert evidence['sdk_reward_points'] == 0
    assert c._call.call_count == 1
    read.assert_not_called()

def test_comment_audit_is_bounded_and_continues_without_writes(monkeypatch):
    from protocol.taskflow.vivo_appointments import execute
    ctx = context()
    client = Mock()
    client.http.hosts = set()
    targets = [{'site': 'wk2609303c903cd9', 'kind': 'comment', 'id': str(i)} for i in range(10)]
    monkeypatch.setattr('protocol.taskflow.vivo_appointments.discover', lambda *args: {'todo': [], 'targets': targets})
    checked = []

    def check(c, target):
        checked.append(target['id'])
        return {'name': '本人评论状态', 'status': 'skipped', 'message': '无本人评论，未代写'}
    monkeypatch.setattr('protocol.taskflow.vivo_appointments.comment_state', check)
    first = execute(Mock(), client, ctx, 'appointment_comments', ctx.checkpoint)
    assert len(checked) == 8 and first.continue_after == 1
    second = execute(Mock(), client, ctx, 'appointment_comments', ctx.checkpoint)
    assert checked == [str(i) for i in range(10)] and second.continue_after is None
    client._call.assert_not_called()

def test_partial_native_progress_continues_instead_of_recursive_execution(monkeypatch):
    ctx = context()
    driver = Mock()
    driver.operate_task.return_value = {'physical_video_played': True, 'observed_seconds': 30}
    monkeypatch.setattr('protocol.taskflow.vivo_browser_tasks.BrowserActions', lambda _: driver)
    task = {'id': 71, 'name': '观看视频', 'completedTimes': 0, 'totalTime': 300}
    adapter = Mock()
    adapter.profile.return_value = SimpleNamespace(snapshot={'points': 100})
    result = execute_one(adapter, Mock(), ctx, task, 'video', ctx.checkpoint, lambda: [{**task, 'completedTimes': 30}], 'ec_browser')
    assert result.continue_after == 1
    adapter.task_group.assert_not_called()
    driver.operate_task.assert_called_once()

def test_member_slider_is_not_disguised_as_account_eligibility_or_retried():
    from protocol.taskflow.vivo_rewards import execute
    ctx = context()
    Journal(ctx, 'vivo_mall_rewards').put('member:1', {'state': 'rejected', 'date': timezone.localdate().isoformat(), 'provider_code': '21057', 'provider_message': '请输入滑块验证码', 'provider_result_status': 'failed'})
    client = Mock()

    def member(path, **kwargs):
        if path == '/home/page/v5/queryInfo.do':
            return {'data': {'zoneInfo': {'zoneContentInfoList': [{'zoneCommodityInfoList': [{'id': 1, 'name': '官方免费权益', 'userStatus': 0, 'price': 0}]}]}}}
        assert path == '/home/page/v5/homePageZoneInfo.do'
        return {'data': {'zoneContentInfoList': []}}
    client.member.side_effect = member
    result = execute(Mock(), client, ctx, 'member_benefits', ctx.checkpoint)
    assert result.status == 'action_required'
    assert result.diagnostics['entries'][0]['reason_kind'] == 'verification'
    assert not any((c.kwargs.get('writes') for c in client.member.call_args_list))

def test_treasure_reads_current_discovery_and_never_requests_historical_59(monkeypatch):
    from protocol.taskflow.vivo_activities import execute
    client = Mock()
    client.http.hosts = set()
    monkeypatch.setattr('protocol.taskflow.vivo_source_flows.crowdfunding_config', lambda _: [])
    result = execute(Mock(), client, context(), 'treasure_status', {'points': 100})
    assert result.status == 'skipped' and result.diagnostics['skip_kind'] == 'unavailable'
    client._call.assert_not_called()

@pytest.mark.parametrize('returned', [20, 19])
def test_weekly_codes_use_verified_bounded_batch_and_match_both_ledgers(monkeypatch, returned):
    from protocol.taskflow.vivo_earn import free_chance
    api = Mock()
    api.config.return_value = {'activityId': 'current'}
    api.weekly_available.side_effect = [{'availableCount': 25, 'luckyNoAmount': 7}, {'availableCount': 5, 'luckyNoAmount': 27}]
    api.weekly_exchange.return_value = [str(i) for i in range(returned)]
    monkeypatch.setattr('protocol.taskflow.vivo_earn.Activities', lambda _: api)
    adapter = Mock()
    adapter.profile.return_value = SimpleNamespace(snapshot={'points': 100})
    ctx = context()
    if returned == 19:
        with pytest.raises(UnknownWriteResult):
            free_chance(adapter, Mock(), ctx, 'weekly_codes', ctx.checkpoint)
        assert any((v['state'] == 'pending' for v in ctx.credentials['scopes']['_taskflow_vivo_free_chances'].values()))
    else:
        result = free_chance(adapter, Mock(), ctx, 'weekly_codes', ctx.checkpoint)
        assert result.status == 'success' and result.continue_after == 1 and (result.snapshot['points'] == 100)
    api.weekly_exchange.assert_called_once_with(count=20)

def test_community_upstream_error_is_never_converted_to_zero_balance(monkeypatch):
    from protocol.taskflow.vivo_source_market import bean_state
    failed = Mock(side_effect=ActionRequired('社区接口异常', provider_code='29999', reason_kind='provider_rejected'))
    monkeypatch.setattr('protocol.taskflow.vivo_source_market.call', failed)
    with pytest.raises(ProtocolError) as caught:
        bean_state(Mock())
    assert caught.value.diagnostics['reason_kind'] == 'provider_unavailable'
    assert caught.value.diagnostics['provider_code'] == '29999'

def test_legacy_sdk_aliases_do_not_duplicate_automatic_client_jobs():
    from protocol.taskflow.vivo_client_tasks import TASKS as clients
    from protocol.taskflow.vivo_sdk_tasks import TASKS as legacy
    assert all((t.automatic for t in clients))
    assert all((not t.automatic for t in legacy))
    assert {t.key.removeprefix('sdk_') for t in legacy} == {t.key.removeprefix('client_tasks_') for t in clients}
