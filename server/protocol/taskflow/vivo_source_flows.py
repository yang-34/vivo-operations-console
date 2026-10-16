# Bundled vivo protocol reference; see source-manifest.json.
"""Remaining reviewed Qinglong branches: participation, claims and reminders."""
import time
from types import SimpleNamespace
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, ReadStageError, amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
from .vendor.vivo.taskcenter import POINTH5, POINTWAP, SIGNHOST
from .vivo_activities import ACTIVITY, WUKONG, Activities, walk
TASKS = (TaskSpec('game_instant_execute', '秒开游戏参与与达标领奖', True), TaskSpec('game_download_execute', '游戏下载参与与达标领奖', True), TaskSpec('game_reserve_execute', '游戏预约活动参与与达标领奖', True), TaskSpec('sign_notification', '开启本账号签到提醒', True), TaskSpec('guess_rewards', '领取竞猜已中奖积分奖励', True), TaskSpec('guess_subscription', '开启本期竞猜开奖提醒', True), TaskSpec('powerball_subscription', '开启当前彩票开奖提醒', True), TaskSpec('coupon_reminder', '开启下一场免费领券提醒', True), TaskSpec('crowdfunding_codes', '领取积分夺宝已有免费幸运码', True), TaskSpec('lottery_activity_free', '使用普通活动已有免费抽奖次数', True), TaskSpec('wish_favorite', '完成积分商城心愿收藏任务', True), TaskSpec('store_pickup_codes', '查询门店任务与取货码', True), TaskSpec('offline_orders', '查询本人线下订单与核销条件'), TaskSpec('bbs_beans_status', '查询社区金豆及兑换条件'), TaskSpec('quiz_rank_prizes', '查询答题榜实物奖与收货资格'), TaskSpec('community_engagement_catalog', '查询社区活动帖与待确认互动'))

def current_row(data, names):
    if isinstance(data, list):
        return rows(data)
    for name in names:
        if isinstance(obj(data).get(name), list):
            return rows(data[name])
    raise ProtocolError('官方没有返回完整的当前清单。')

def call(client, path, body=None, *, host=POINTH5, method='GET', writes=False):
    return client._call(host, path, params=body or {}, method=method, writes=writes, pkg='com.vivo.space', with_vivo_account=True, common=False)

def execute(adapter, client, context, key, snapshot):
    if key.startswith('game_'):
        return game_participation(adapter, client, context, key, snapshot)
    if key == 'crowdfunding_codes':
        return crowdfunding(adapter, client, context, snapshot)
    if key == 'lottery_activity_free':
        return ordinary_lottery(adapter, client, context, snapshot)
    if key == 'wish_favorite':
        return wish_favorite(adapter, client, context, snapshot)
    if key == 'store_pickup_codes':
        return store_codes(client, context, snapshot)
    if key in ('sign_notification', 'guess_subscription', 'powerball_subscription', 'coupon_reminder'):
        return remind(adapter, client, context, key, snapshot)
    if key == 'guess_rewards':
        return guess_rewards(adapter, client, context, snapshot)
    if key == 'offline_orders':
        try:
            data = call(client, '/offline/api/task/query.do').get('data')
        except ActionRequired as error:
            if error.diagnostics.get('provider_code') != '12211':
                raise
            raise ActionRequired('官方确认该线下核销入口要求门店导购身份，当前消费者账号不能使用该入口。', provider_code='12211', reason_kind='eligibility') from error
        return StepResult('success', '本人线下订单已查询；订单核销须由实际门店导购完成', snapshot, diagnostics={'entries': [{'name': '线下订单', 'status': 'success', 'message': '订单清单已查询' if data else '当前没有待核销订单'}]})
    if key == 'bbs_beans_status':
        from .vivo_source_market import bean_state
        state = bean_state(client)
        return StepResult('success', f"本人社区金豆{state['beans']}，资产转换可在兑换与消耗逐笔确认", snapshot)
    if key in ('quiz_rank_prizes', 'community_engagement_catalog'):
        from .vivo_source_market import catalog
        provider = SimpleNamespace(client=client, snapshot=snapshot, credentials=client.credentials)
        offers = catalog(provider, family='rank' if key == 'quiz_rank_prizes' else 'community')
        return StepResult('success', f'已查询{len(offers)}项本人可确认的免费操作；选择收货地址或互动对象后可在兑换与消耗执行', snapshot)
    raise ProtocolError('未支持的新增 vivo 流程。')

def game_participation(adapter, client, context, key, snapshot):
    kind = key.removeprefix('game_').removesuffix('_execute')
    mode = {'instant': 0, 'download': 1, 'reserve': 2}[kind]
    reward = client.PLAY_METHOD_REWARD_KEY[mode]
    data = obj(client.game_list(mode).get('data'))
    if not isinstance(data.get('games'), list):
        raise ProtocolError('当前游戏参与清单不完整。')
    journal, entries, processed, more = (Journal(context, 'vivo_game_participation'), [], 0, False)
    for game in rows(data['games']):
        state = str(obj(game.get(reward)).get('status'))
        if state in ('1', '2'):
            continue
        gid, pkg, token = (game.get('id'), game.get('pkgName'), game.get('token'))
        if not gid or not pkg or (not token):
            entries.append({'name': title(game.get('name')), 'status': 'action_required', 'message': '官方尚未下发此游戏的参与票据', 'reason_kind': 'missing_context'})
            continue
        rid = f'{mode}:{gid}:{timezone.localdate()}'
        if journal.saved(rid):
            continue
        if processed >= 3:
            more = True
            break
        journal.submit(rid, lambda: client.game_upload(game, stat=4, play_method=mode), before=state)
        updated = client.game_reward_status({'pkgName': pkg, 'gameCfgId': gid}).get('data')
        journal.confirm(rid)
        client.http.writing = False
        entries.append({'name': title(game.get('name')), 'status': 'success' if str(obj(updated).get('status')) in ('1', '2') else 'action_required', 'message': '官方确认已达标，继续领取奖励' if str(obj(updated).get('status')) in ('1', '2') else '参与上报已受理，官方尚未确认游戏前置达标', 'reason_kind': '' if str(obj(updated).get('status')) in ('1', '2') else 'eligibility'})
        processed += 1
    claimed = adapter.games(client, context, 'game_' + kind, snapshot)
    current = adapter.profile(client).snapshot
    if current['points'] < snapshot['points']:
        raise UnknownWriteResult
    entries.extend(claimed.diagnostics.get('entries', []))
    result = report({**snapshot, **current}, entries, '游戏参与、达标状态与领奖已逐项核对')
    return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=1 if more or claimed.continue_after is not None else None)

def crowdfunding_config(client):
    from .vivo_updates import site_config, sites
    api = Activities(client)
    candidates = []
    for site in sites(client):
        config = site_config(api, site)
        if config is None:
            continue
        for node in walk(config):
            name = ' '.join((str(v or '') for v in (node.get('name'), node.get('componentName'), obj(node.get('pluginInfo')).get('enName')))).lower()
            if 'crowdfunding' not in name:
                continue
            ids = {str(v['activityId']) for v in walk(node) if v.get('activityId')}
            candidates.extend(((site, aid) for aid in ids))
    return list(dict.fromkeys(candidates))

def crowdfunding(adapter, client, context, snapshot):
    api, journal, entries = (Activities(client), Journal(context, 'vivo_crowdfunding'), [])
    for site, aid in crowdfunding_config(client):
        params = {'activityId': aid, 'siteId': site}

        def read(suffix):
            return api.call(WUKONG, '/wukongapc/crowdfunding/' + suffix, params, site=site)
        config = obj(read('config/query'))
        if str(config.get('activityStatus')) == '2':
            entries.append({'name': '夺宝幸运码', 'status': 'skipped', 'message': '官方确认本期已结束'})
            continue
        state = obj(read('lucky/available'))
        count, held = (amount(state.get('availableCount')), amount(state.get('luckyNoAmount', 0)))
        if not count:
            continue
        before = adapter.profile(client).snapshot['points']
        rid = f'{aid}:{timezone.localdate()}:{count}'
        receipt = journal.submit(rid, lambda: api.call(WUKONG, '/wukongapc/crowdfunding/lucky/exchange', {**params, 'count': 1, 'hasUserLocation': False, 'province': '', 'city': ''}, method='POST', writes=True, site=site), before={'count': count, 'held': held, 'points': before})
        after = obj(read('lucky/available'))
        current = adapter.profile(client).snapshot
        if not isinstance(receipt, list) or len(receipt) != 1 or amount(after.get('availableCount')) != count - 1 or (amount(after.get('luckyNoAmount')) != held + 1) or (current['points'] < before):
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        snapshot = {**snapshot, **current}
        entries.append({'name': '夺宝幸运码', 'status': 'success', 'message': '免费次数减少1、持有码增加1已回读，积分未扣减'})
        result = report(snapshot, entries, '已有免费幸运码已领取')
        return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if count > 1 else None)
    return report(snapshot, entries, '当期夺宝站点与免费幸运码已核对')

def remind(adapter, client, context, key, snapshot):
    journal = Journal(context, 'vivo_reminders')
    api = Activities(client)
    if key == 'coupon_reminder':
        from .vivo_activity_market import catalog
        from .vivo_activity_market import execute as confirmed
        provider = SimpleNamespace(client=client, credentials=client.credentials, snapshot=snapshot, balance=lambda: adapter.profile(client).snapshot['points'])
        offers = catalog(provider, 'vivo:coupon-remind')
        if not offers:
            return StepResult('skipped', '当前没有待开抢的可提醒场次', snapshot)
        proposal = offers[0]
        if proposal['cost'] != '0' or proposal['payload'].get('kind') != 'remind':
            raise ActionRequired('当前提醒包含消耗，未提交。')
        target = proposal['payload']
        rid = 'coupon:v2:' + ':'.join((str(target[k]) for k in ('activityId', 'tabId', 'couponUuid')))
        if journal.saved(rid).get('state') == 'confirmed':
            return StepResult('skipped', '本场提醒已核验，不重复设置', snapshot)
        result = journal.submit(rid, lambda: confirmed(provider, proposal), before={'activityId': target['activityId'], 'tabId': target['tabId'], 'couponUuid': target['couponUuid'], 'isRemind': 1, 'points_cost': 0})
        journal.confirm(rid)
        return result
    if key == 'sign_notification':
        rid = 'sign:' + timezone.localdate().isoformat()

        def sign_state():
            return obj(client._call(SIGNHOST, '/api/sign/info.do', params={'sysCode': 'wallet'}, pkg='com.vivo.wallet', writes=False).get('data'))
        before_notify = sign_state()
        if 'personSginNotify' not in before_notify:
            raise ProtocolError('官方没有返回本账号的签到提醒状态。')
        if yes(before_notify['personSginNotify']):
            return StepResult('skipped', '本账号签到提醒已开启', snapshot)

        def sender():
            return client._call(SIGNHOST, '/api/sign/notify/notify.do', method='POST', params={'notifyFlag': 1}, pkg='com.vivo.wallet', writes=True)
    else:
        if key == 'guess_subscription':
            state = api.guess()
            aid = state.get('id')
            kind = 3
        else:
            state = obj(api.powerball('baseInfo'))
            aid = obj(state.get('powerBallActivityVO')).get('activityId')
            kind = 6
        if not aid:
            return StepResult('skipped', '官方当前没有发布可订阅的场次', snapshot)
        rid = f'{kind}:{aid}'

        def sender():
            return api.call(ACTIVITY, '/api/activity/subscribe.do', {'activityType': kind, 'activityId': aid, 'type': 1}, method='POST', writes=True)
    if journal.saved(rid).get('state') == 'confirmed':
        return StepResult('skipped', '本项提醒已受理，不重复设置', snapshot)
    journal.submit(rid, sender)
    if key == 'sign_notification' and (not yes(sign_state().get('personSginNotify'))):
        raise UnknownWriteResult
    current = adapter.profile(client).snapshot
    if current['points'] < snapshot['points']:
        raise UnknownWriteResult
    journal.confirm(rid)
    client.http.writing = False
    return StepResult('success', '官方已受理本账号提醒设置，积分未扣减；提醒设置不计为积分奖励', {**snapshot, **current})

def guess_rewards(adapter, client, context, snapshot):
    api = Activities(client)
    state = api.guess()
    if not state.get('id'):
        return StepResult('skipped', '当前没有竞猜待结算场次', snapshot)
    if state.get('isChipIn') is not None and not yes(state['isChipIn']):
        return StepResult('skipped', '当前账号未参与本场竞猜，没有可领取的竞猜奖励', snapshot)
    journal = Journal(context, 'vivo_guess_rewards')
    rid = f"{state['id']}:{timezone.localdate()}"
    if journal.saved(rid).get('state') == 'confirmed':
        return StepResult('skipped', '本场竞猜奖励已核验', snapshot)
    before = adapter.profile(client).snapshot['points']
    try:
        journal.submit(rid, lambda: call(client, '/api/activity/guess/v2/receive.do', writes=True))
    except ActionRequired as e:
        client.http.writing = False
        return StepResult('action_required', '官方尚未确认本账号有竞猜待领奖励', snapshot, diagnostics={**e.diagnostics, 'reason_kind': 'eligibility'})
    current = adapter.profile(client).snapshot
    if current['points'] < before:
        raise UnknownWriteResult
    if current['points'] == before:
        client.http.writing = False
        return StepResult('action_required', '竞猜领奖已受理，尚未核验积分到账；保留记录、不重复领取', {**snapshot, **current}, diagnostics={'reason_kind': 'settlement'})
    journal.confirm(rid)
    client.http.writing = False
    return StepResult('success', f"竞猜奖励积分增加{current['points'] - before}已回读", {**snapshot, **current})

def ordinary_lottery(adapter, client, context, snapshot):
    data = obj(call(client, '/api/lottery/list.do').get('data'))
    if not isinstance(data.get('activities'), list):
        raise ProtocolError('普通抽奖活动清单不完整。')
    journal, entries = (Journal(context, 'vivo_ordinary_lottery'), [])
    for activity in rows(data['activities']):
        now = time.time() * 1000
        if not int(activity.get('startTime', 0)) <= now < int(activity.get('endTime', 0)):
            continue
        aid = activity.get('id')
        if not aid:
            raise ProtocolError('普通抽奖活动缺少当前编号。')
        detail = obj(call(client, '/api/lottery/detail.do', {'actId': aid}).get('data'))
        state = obj(call(client, '/api/lottery/userStats.do', {'actId': aid}).get('data'))
        if state.get('freeTimes') is None:
            entries.append({'name': title(activity.get('name')), 'status': 'action_required', 'message': '当前未下发明确免费次数，未使用积分抽取', 'reason_kind': 'manual'})
            continue
        count = amount(state['freeTimes'])
        if not count:
            continue
        fields = {k: detail.get(k, state.get(k)) for k in ('scene', 'ticket', 'constId')}
        if any((v is None for v in fields.values())):
            entries.append({'name': title(activity.get('name')), 'status': 'action_required', 'message': '官方当前场次缺少完整安全验证资料', 'reason_kind': 'missing_context'})
            continue
        before = adapter.profile(client).snapshot['points']
        rid = f'{aid}:{timezone.localdate()}:{count}'
        journal.submit(rid, lambda: call(client, '/api/lottery/draw.do', {'actId': aid, **fields}, method='POST', writes=True), before={'count': count, 'points': before})
        after = obj(call(client, '/api/lottery/userStats.do', {'actId': aid}).get('data'))
        current = adapter.profile(client).snapshot
        if amount(after.get('freeTimes')) != count - 1 or current['points'] < before:
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        snapshot = {**snapshot, **current}
        entries.append({'name': title(activity.get('name')), 'status': 'success', 'message': '本次免费次数扣减及积分未减少已回读，中奖结果以官方记录为准'})
        result = report(snapshot, entries, '普通活动免费抽取已核验')
        return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1)
    return report(snapshot, entries, '当前普通抽奖活动已逐项核对')

def wish_favorite(adapter, client, context, snapshot):
    tasks = client.group_list('2')
    target = [t for t in tasks if '收藏' in str(t.get('name', '')) and '商品' in str(t.get('name', '')) and (not yes(t.get('taskComplete'))) and (amount(t.get('completedTimes') or 0) < amount(t.get('totalTime') or 1))]
    if not target:
        return StepResult('skipped', '当前没有未完成的商城商品收藏任务', snapshot)
    try:
        listing = call(client, '/api/wish/list.do').get('data')
    except ReadStageError as error:
        if error.diagnostics.get('provider_code') != 'HTTP_404':
            raise
        from .vivo_native_earn import execute as shop_favorite
        result = shop_favorite(adapter, client, context, 'shop_favorite', snapshot)
        return StepResult(result.status, '旧心愿接口已下线，当前商品收藏流程已核验；' + result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=result.continue_after)
    previous = current_row(listing, ('items', 'list', 'wishes', 'wishList'))
    if previous:
        return StepResult('skipped', '已有本人心愿商品，继续由商城收藏任务核验任务进度', snapshot)
    categories = obj(call(client, '/api/mall/v5/home/categories.do').get('data'))
    product = None
    for cat in rows(categories.get('categories')):
        values = obj(call(client, '/api/mall/v5/home/items.do', {'categoryId': cat.get('id')}).get('data'))
        product = next((p for p in rows(values.get('items')) if p.get('id') and p.get('playMethod') == 0), None)
        if product:
            break
    if not product:
        return StepResult('skipped', '当前没有已发布的可收藏商品', snapshot)
    journal = Journal(context, 'vivo_wish')
    rid = str(product['id'])
    before = adapter.profile(client).snapshot['points']
    journal.submit(rid, lambda: call(client, '/api/wish/add.do', {'mallItemId': product['id']}, method='POST', writes=True))
    after = current_row(call(client, '/api/wish/list.do').get('data'), ('items', 'list', 'wishes', 'wishList'))
    current = adapter.profile(client).snapshot
    if not any((str(r.get('mallItemId', r.get('id'))) == rid for r in after)) or current['points'] < before:
        raise UnknownWriteResult
    journal.confirm(rid)
    client.http.writing = False
    return StepResult('success', '本人心愿清单新增商品已回读；收藏不计为积分到账', {**snapshot, **current})

def store_codes(client, context, snapshot):
    from .vivo_store_location import presentation, resolve
    supplied = obj(obj(client.credentials.get('scopes')).get('vivo_inputs'))
    location, reference, source = resolve(client, supplied)
    if location is None:
        return StepResult('skipped', '官方地图当前地区未发布可查询门店', snapshot)
    data = call(client, '/api/store/selected/list.do', {'lng': str(location['longitude']), 'lat': str(location['latitude']), 'city': location['city'], 'pageNo': 1, 'pageSize': 20, 'from': 'EC', 'businessCode': 'vivop', 'vkCityId': supplied.get('vkCityId', '')}, host=POINTWAP, method='POST').get('data')
    stores = current_row(data, ('stores', 'list'))
    sid = supplied.get('store_id') or obj(reference).get('thirdOrganizationId') or obj(reference).get('storeId')
    code = supplied.get('store_code') or obj(reference).get('storeCode')
    selected = next((s for s in stores if (not sid or str(s.get('storeId')) == str(sid)) and (not code or s.get('storeCode') == code)), None)
    if not selected:
        if sid or code:
            raise ActionRequired('官方积分入口当前未开放该地图门店的任务。', reason_kind='unavailable')
        return StepResult('skipped', '当前位置未返回开放任务的门店', snapshot)
    if not selected.get('storeId') or not selected.get('storeCode'):
        raise ProtocolError('官方门店资料缺少任务上下文。')
    snapshot = {**snapshot, 'store_task_location': presentation(selected, location, source)}
    tasks = obj(call(client, '/api/task/ec/taskList.do', {'from': 'EC', 'type': 1, 'businessCode': 'vivop', 'tasks': '', 'storeId': str(selected['storeId']), 'storeCode': selected['storeCode']}, host=POINTWAP).get('data'))
    if not isinstance(tasks.get('tasks'), list):
        raise ProtocolError('官方门店任务清单不完整。')
    entries = []
    journal = Journal(context, 'vivo_store_codes')

    def keep_code(task, code):
        row = {'store': title(selected.get('name')), 'task': title(task.get('name')), 'code': code, 'date': timezone.localdate().isoformat()}
        previous = snapshot.get('store_task_codes', [])
        snapshot['store_task_codes'] = [item for item in previous if any((item.get(k) != row[k] for k in ('store', 'task', 'date')))] + [row]
    for task in rows(tasks.get('tasks')):
        if str(task.get('taskSource')) != '3':
            continue
        tid = task.get('id')
        if not tid or str(tid) == '0':
            purchase = str(task.get('subType')) in ('301', '306')
            entries.append({'name': title(task.get('name')), 'status': 'action_required', 'message': '需要该店真实购买订单与导购核销；官方未投放可生成取货码的任务编号' if purchase else '官方当前仅下发占位任务编号，未套用历史取货码任务号', 'reason_kind': 'eligibility' if purchase else 'missing_context'})
            continue
        rid = f"{selected['storeId']}:{tid}:{timezone.localdate()}"
        if journal.saved(rid).get('state') == 'confirmed':
            if journal.saved(rid).get('code'):
                keep_code(task, journal.saved(rid)['code'])
                entries.append({'name': title(task.get('name')), 'status': 'action_required', 'message': '本人取货码已保留，仍须由实际门店导购扫码核销', 'reason_kind': 'manual'})
            continue
        value = journal.submit(rid, lambda: call(client, '/api/task/refreshStoreTaskCode.do', {'taskId': tid, 'noLoading': True}, host=POINTWAP, writes=True))
        if not obj(value.get('data')).get('code'):
            raise UnknownWriteResult
        code = str(obj(value.get('data'))['code'])
        journal.put(rid, {'state': 'confirmed', 'date': timezone.localdate().isoformat(), 'code': code})
        keep_code(task, code)
        client.http.writing = False
        entries.append({'name': title(task.get('name')), 'status': 'action_required', 'message': '本人取货码已取得并加密保留，须由实际门店导购扫码核销', 'reason_kind': 'manual'})
    result = report(snapshot, entries, '门店任务已按官方地图位置核对；到店和订单条件按官方核验')
    reasons = {row.get('reason_kind') for row in entries if row['status'] == 'action_required'}
    return StepResult(result.status, result.message, result.snapshot, diagnostics={**result.diagnostics, 'store_context': snapshot['store_task_location'], **({'reason_kind': next(iter(reasons))} if len(reasons) == 1 else {'reason_kind': 'partial'} if reasons else {})})
