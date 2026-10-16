# Bundled vivo protocol reference; see source-manifest.json.
"""Account-owned free earnings: pending tasks, current sign sites and free chances."""
from .compat import timezone
from .added_common import Journal, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
from .vivo_activities import Activities
from .vivo_activity_market import available_count
TASKS = (TaskSpec('news_rewards', '自动阅读资讯与分档领奖', True), TaskSpec('reservation_rewards', '游戏预约执行与积分核验', True), TaskSpec('customized_signs', '当前悟空站点业务签到', True), TaskSpec('golden_free', '金蛋每日免费机会', True), TaskSpec('double11_free', '双11每日免费积分机会', True), TaskSpec('weekly_codes', '周周幸运抽领取已有免费幸运码', True), TaskSpec('shop_favorite', '收藏商品积分任务', True), TaskSpec('community_reads', '社区文章阅读积分任务', True))

def execute(adapter, client, context, key, snapshot):
    if key == 'news_rewards':
        from .vivo_news import execute as read_news
        return read_news(adapter, client, context, snapshot)
    if key == 'reservation_rewards':
        from .vivo_game_reservation import execute as reserve
        ordinary = reserve(adapter, client, context, snapshot)
        games = adapter.games(client, context, 'game_reserve', ordinary.snapshot)
        entries = ordinary.diagnostics.get('entries', []) + games.diagnostics.get('entries', [])
        if not games.diagnostics.get('entries'):
            entries.append({'name': '预约积分专题奖励', 'status': 'skipped', 'message': '当前积分专题未发布可领取的预约奖励'})
        result = report(games.snapshot, entries, '预约任务与当前游戏活动奖励已逐项回读')
        return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=ordinary.continue_after or games.continue_after)
    if key == 'customized_signs':
        return customized_signs(adapter, client, context, snapshot)
    if key in ('shop_favorite', 'community_reads'):
        from .vivo_native_earn import execute as native_execute
        return native_execute(adapter, client, context, key, snapshot)
    return free_chance(adapter, client, context, key, snapshot)

def free_chance(adapter, client, context, key, snapshot):
    family = {'golden_free': 'golden', 'double11_free': 'double11', 'weekly_codes': 'weekly'}.get(key)
    if not family:
        raise ProtocolError('没有对应的免费积分活动。')
    api, journal = (Activities(client), Journal(context, 'vivo_free_chances'))
    config = api.config(family)
    state = api.weekly_available() if family == 'weekly' else api.available(family)
    count = amount(state.get('availableCount')) if family == 'weekly' else available_count(state)
    name = {'golden': '金蛋', 'double11': '双11', 'weekly': '周周幸运抽'}[family]
    if count == 0:
        return StepResult('skipped', name + '当前没有剩余免费机会', snapshot, diagnostics={'skip_kind': 'no_reward'})
    before = adapter.profile(client).snapshot['points']
    quantity = min(count, 20) if family == 'weekly' else 1
    rid = f"{family}:{config['activityId']}:{timezone.localdate()}:{count}"
    receipt = journal.submit(rid, (lambda: api.weekly_exchange(count=quantity)) if family == 'weekly' else lambda: api.draw(family), before={'count': count, 'points': before, 'quantity': quantity})
    after = api.weekly_available() if family == 'weekly' else api.available(family)
    remaining = amount(after.get('availableCount')) if family == 'weekly' else available_count(after)
    current = adapter.profile(client).snapshot
    if remaining != count - quantity or current['points'] < before:
        raise UnknownWriteResult
    if family == 'weekly' and (not isinstance(receipt, list) or len(receipt) != quantity or amount(after.get('luckyNoAmount')) != amount(state.get('luckyNoAmount')) + quantity):
        raise UnknownWriteResult
    journal.confirm(rid)
    client.http.writing = False
    message = f'已领取{quantity}个幸运码，持有码和免费次数均已回读' if family == 'weekly' else f"已使用1次{name}免费机会，积分增加{current['points'] - before}分，剩余{remaining}次"
    return StepResult('success', message, {**snapshot, **current}, continue_after=1 if remaining else None)

def customized_signs(adapter, client, context, snapshot):
    from .vivo import BIZ_PACKAGE, POINTH5, SIGNHOST

    def read():
        data = client._call(POINTH5, '/api/mall/task/center/new/signBizList.do', pkg='com.vivo.space').get('data')
        if not isinstance(data, list):
            raise ProtocolError('当前业务签到表未完整返回。')
        return rows(data)
    entries, journal, count = ([], Journal(context, 'vivo_customized_signs'), 0)
    for row in read():
        name, code = (title(row.get('bizName')), str(row.get('bizCode') or ''))
        if yes(row.get('isSign')):
            entries.append({'name': name, 'status': 'skipped', 'message': '当前业务签到表确认今日已签'})
            continue
        if code not in BIZ_PACKAGE or not str(row.get('selfJumpUrl', '')).startswith('https://zhan.vivo.com.cn/point/wk'):
            entries.append({'name': name, 'status': 'skipped', 'message': '当前业务未发布可执行的悟空签到站点'})
            continue
        if count >= 3:
            result = report(snapshot, entries, '当前签到站点已核对，继续剩余业务')
            return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=1)
        rid = code + ':' + timezone.localdate().isoformat()
        try:
            journal.submit(rid, lambda: client._call(SIGNHOST, '/api/sign/customize/sign.do', method='POST', form=True, pkg=BIZ_PACKAGE[code], with_vivo_account=True, common=False, params={'sysCode': code}, referer=row['selfJumpUrl']))
        except ActionRequired as exc:
            if exc.diagnostics.get('provider_code') == '10002':
                client.http.writing = False
                entries.append({'name': name, 'status': 'skipped', 'message': '当前站点没有进行中的签到活动'})
                continue
            raise
        matched = next((r for r in read() if r.get('bizCode') == code), {})
        if not yes(matched.get('isSign')):
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        current = adapter.profile(client).snapshot
        entries.append({'name': name, 'status': 'success', 'message': f"站点签到已回读，积分余额变化{current['points'] - snapshot['points']}分"})
        snapshot = {**snapshot, **current}
        count += 1
    return report(snapshot, entries, '当前悟空签到站点已逐项核对')
