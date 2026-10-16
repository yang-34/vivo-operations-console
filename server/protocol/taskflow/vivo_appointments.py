# Bundled vivo protocol reference; see source-manifest.json.
"""Published new-product activities: sign, real visits, free reservations and draws."""
import hashlib
import re
import time
from copy import deepcopy
from urllib.parse import urlparse
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, ReadStageError, amount, decimal_amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
from .vivo_activities import WUKONG, Activities, walk
from .vivo_native_earn import Native
from .vivo_updates import site_config, sites
SHOP = 'https://mshopact.vivo.com.cn'
BBS = 'https://bbs.vivo.com.cn'
SEEDS = {'wk260928c41ab86a'}
CACHE = '_taskflow_vivo_appointment_catalog'
TASKS = (TaskSpec('appointment_catalog', '查询新品活动签到、预约与抽奖资格'), TaskSpec('appointment_sign', '新品活动每日签到领积分', True), TaskSpec('appointment_browse', '完成新品活动浏览任务并领取免费次数', True), TaskSpec('appointment_reserve', '完成新品活动0元预约', True), TaskSpec('appointment_draw', '使用新品活动已有免费抽奖次数', True), TaskSpec('appointment_comments', '核验新品评论、真实评价与抽奖资格'), TaskSpec('appointment_reminder', '开启新品活动签到提醒', True))

class AppointmentAPI(Activities):
    """MSA writes use the official form and the owned, verified shop session."""
    native = None

    def prepare_shop(self):
        if self.native is None:
            self.native = Native(self.client)
            self.native.shop_session()

    def call(self, host, path, params=None, *, writes=False, method='GET', headers=None, site=None):
        if host != SHOP or (not writes and self.native is None):
            return super().call(host, path, params, writes=writes, method=method, headers=headers, site=site)
        self.prepare_shop()
        if method == 'POST':
            return self.native.call(host, path, method=method, form=params or {}, writes=writes)
        return self.native.call(host, path, method=method, params=params or {}, writes=writes)

def published_targets(site, config):
    """Keep plugin IDs separate: sign IDs, lottery IDs and community thread IDs differ."""
    found = {}
    for item in walk(config):
        plugin = obj(item.get('pluginInfo')).get('enName', '')
        pc = obj(item.get('pluginConfig'))
        if not plugin or not pc:
            continue
        candidates = []
        if plugin == 'msa-appoint':
            for field in ('lotterySetting', 'launchLotterySetting'):
                setting = obj(pc.get(field))
                if setting.get('lotteryId'):
                    candidates.append(('reservation', str(setting['lotteryId'])))
            setting = obj(pc.get('appontSetting'))
            if setting.get('appointId'):
                candidates.append(('reserve_only', str(setting['appointId'])))
        elif 'sign-front' in plugin:
            ids = {str(n['activityId']) for n in walk(pc) if n.get('activityId')}
            if len(ids) == 1:
                candidates.append(('sign', ids.pop()))
        elif 'lottery' in plugin.lower() and plugin.startswith(('wukong-', 'scrm-')):
            ids = {str(n['activityId']) for n in walk(pc) if n.get('activityId')}
            if len(ids) == 1:
                candidates.append(('lottery', ids.pop()))
        elif plugin == 'msa-community-comment' and pc.get('activityId'):
            candidates.append(('comment', str(pc['activityId'])))
        elif plugin == 'site-nps-comment' and pc.get('npsActivityId'):
            candidates.append(('nps', str(pc['npsActivityId'])))
        for kind, identifier in candidates:
            if re.fullmatch('[A-Za-z0-9-]{1,80}', identifier):
                record = {'site': site, 'kind': kind, 'id': identifier, 'plugin': plugin}
                if kind == 'reservation':
                    record['launch'] = str(obj(pc.get('launchLotterySetting')).get('lotteryId', '')) == identifier
                found[site, kind, identifier] = record
    return list(found.values())

def apc(client, target, path, extra=None, *, writes=False, form=False):
    params = {'activityId': target['id'], 'siteId': target['site'], **(extra or {})}
    if path == 'signin/prompting/set':
        params.pop('siteId')
    return client._call(WUKONG, '/wukongapc/' + path, params=params, method='POST' if writes else 'GET', writes=writes, common=False, pkg='com.vivo.space', with_vivo_account=True, cookie_profile='wukong_appointment', form=form, referer='https://zhan.vivo.com.cn/vivospace/' + target['site']).get('data')

def discover(client, context):
    today = timezone.localdate().isoformat()
    cached = obj(context.credentials.get('scopes', {}).get(CACHE))
    if cached.get('date') == today and cached.get('version') == 1:
        cached['targets'] = [t for t in cached.get('targets', []) if not (t.get('kind') == 'lottery' and t.get('plugin', '').startswith('point-'))]
        cached['version'] = 2
    if cached.get('date') != today or cached.get('version') != 3:
        candidates = SEEDS | {s for s in sites(client) if re.fullmatch('wk[0-9a-z]{10,16}', s)}
        cached = {'date': today, 'version': 3, 'todo': sorted(candidates), 'targets': [], 'unavailable': [], 'visited': []}
    api = Activities(client)
    for site in cached['todo'][:12]:
        try:
            config = site_config(api, site)
            if config:
                cached['targets'].extend(published_targets(site, config))
                children = set()
                for node in walk(config):
                    for value in node.values():
                        if not isinstance(value, str) or not value.startswith('https://'):
                            continue
                        try:
                            parsed = urlparse(value)
                        except ValueError:
                            continue
                        if parsed.scheme == 'https' and parsed.hostname in ('zhan.vivo.com.cn', 'mshopact.vivo.com.cn'):
                            children.update(re.findall('wk[0-9a-z]{10,16}', parsed.path))
                known = set(cached.get('visited', [])) | set(cached['todo'])
                cached['todo'].extend(sorted(children - known))
        except ProtocolError as error:
            cached['unavailable'].append({'site': site, 'code': error.diagnostics.get('provider_code', 'unavailable')})
        cached['todo'].remove(site)
        cached.setdefault('visited', []).append(site)
        cached['targets'] = list({(t['site'], t['kind'], t['id']): t for t in cached['targets']}.values())
        scopes = deepcopy(context.credentials.get('scopes', {}))
        scopes[CACHE] = cached
        context.persist_scopes(scopes)
    return cached

def entry(target, status, message, **extra):
    names = {'sign': '新品活动签到', 'reservation': '新品预约抽奖', 'reserve_only': '新品0元预约', 'lottery': '新品活动抽奖', 'comment': '新品评论活动', 'nps': '新品真实评价'}
    return {'name': names.get(target['kind'], '新品活动'), 'status': status, 'message': message, 'site_id': target['site'], **extra}

def sign_state(client, target):
    data = obj(apc(client, target, 'signin/user/info'))
    if data.get('loginFlag') is not True:
        raise ActionRequired('当前活动未识别此账号的登录状态。', reason_kind='identity')
    if str(data.get('activityStatus')) != '1':
        raise ActionRequired('此签到活动当前未开放。', reason_kind='ended')
    if type(data.get('todaySignFlag')) is not bool:
        raise ReadStageError('活动没有返回明确的今日签到标记。')
    return data

def sign_or_remind(client, context, target, key):
    before = sign_state(client, target)
    journal = Journal(context, 'vivo_appointment_sign')
    identifier = target['id'] + ':' + timezone.localdate().isoformat()
    if key == 'appointment_reminder':
        rid = identifier + ':prompt'
        if 'promptingStatus' not in before:
            raise ReadStageError('此签到活动没有返回提醒开关状态。')
        if yes(before['promptingStatus']):
            if journal.saved(rid).get('state') == 'pending':
                journal.confirm(rid)
            return entry(target, 'skipped', '本活动签到提醒已开启')
        if before.get('isEnablePush') is not True:
            return entry(target, 'action_required', '此活动未开放签到提醒', reason_kind='unavailable')
        journal.submit(rid, lambda: apc(client, target, 'signin/prompting/set', {'status': 1}, writes=True))
        after = sign_state(client, target)
        if not yes(after.get('promptingStatus')):
            return entry(target, 'action_required', '提醒设置已提交，后台继续回查，不重复设置', reason_kind='auto_review')
        journal.confirm(rid)
        client.http.writing = False
        return entry(target, 'success', '活动签到提醒已开启并回读确认')
    if before['todaySignFlag']:
        if journal.saved(identifier).get('state') == 'pending':
            journal.confirm(identifier)
        return entry(target, 'skipped', f"今日已签到，周期进度 {before.get('signDays')}/{before.get('cycle')}")
    try:
        receipt = obj(journal.submit(identifier, lambda: apc(client, target, 'signin/sign', writes=True), before={'days': before.get('signDays')}))
    except ActionRequired as error:
        if error.diagnostics.get('provider_code') != '10000':
            raise
        client.http.writing = False
        if not sign_state(client, target)['todaySignFlag']:
            raise
        journal.confirm(identifier)
        return entry(target, 'skipped', '平台确认今日已签到，未重复领取')
    after = sign_state(client, target)
    if not after['todaySignFlag']:
        return entry(target, 'action_required', '签到提交尚未反映到官方状态，后台回查且不重复提交', reason_kind='auto_review')
    journal.confirm(identifier)
    client.http.writing = False
    return entry(target, 'success', '今日签到已确认；官方回执：' + title(receipt.get('rewardName'), '签到成功'))

def shop_state(api, target):
    return obj(api.call(SHOP, '/lottery/memberTaskState', {'uuid': target['id']}, site=target['site']))

def reserve(api, client, context, target):
    state = shop_state(api, target) if target['kind'] == 'reservation' else {}
    if state and str(state.get('lotteryActivityStatus')) != '1':
        return entry(target, 'skipped', '当前预约抽奖未开放')
    tasks = rows(state.get('memberTasks'))
    candidates = [t for t in tasks if str(obj(t.get('activityPrize')).get('activityType')) == '1']
    if target['kind'] == 'reserve_only':
        candidates = [{'activityPrize': {'activityId': target['id']}}]
    if not candidates:
        return entry(target, 'skipped', '本活动没有未完成的免费预约')
    journal = Journal(context, 'vivo_appointment_reserve')
    for task in candidates:
        aid = str(obj(task.get('activityPrize')).get('activityId', ''))
        if not aid.isdigit():
            continue
        rid = 'reserve:' + aid
        if yes(task.get('completeFlag')) or yes(task.get('isAppointment')):
            if journal.saved(rid).get('state') == 'pending':
                journal.confirm(rid)
            return entry(target, 'skipped', '本人已完成该新品预约')
        if target['kind'] == 'reserve_only':
            before = api.call(SHOP, '/appointment/queryRecord', {'activityId': aid}, site=target['site'])
            if before:
                return entry(target, 'skipped', '本人已完成该新品预约')
        info = obj(api.call(SHOP, '/appointment/getAppointment', {'activityId': aid}, site=target['site']))
        if any((decimal_amount(info[name]) > 0 for name in ('price', 'amount', 'pointsCost', 'deposit') if name in info)):
            return entry(target, 'action_required', '此预约存在费用，未参加', reason_kind='eligibility')
        api.prepare_shop()
        journal.submit(rid, lambda: api.call(SHOP, '/appointment/freeAppointment', {'activityId': aid, 'pluginSource': 2}, writes=True, method='POST', site=target['site']))
        if target['kind'] == 'reservation':
            after = any(((yes(t.get('completeFlag')) or yes(t.get('isAppointment'))) and str(obj(t.get('activityPrize')).get('activityId')) == aid for t in rows(shop_state(api, target).get('memberTasks'))))
        else:
            after = api.call(SHOP, '/appointment/queryRecord', {'activityId': aid}, site=target['site'])
        if not after:
            return entry(target, 'action_required', '免费预约结果等待官方确认，不重复预约', reason_kind='auto_review')
        journal.confirm(rid)
        client.http.writing = False
        return entry(target, 'success', '0元预约已完成并回读确认')
    return entry(target, 'skipped', '未下发有效预约编号')

def browse(client, context, target):
    client.http.hosts |= {'scrmact.vivo.com.cn', 'mall.vivo.com.cn', 'shop.vivo.com.cn'}
    data = obj(apc(client, target, 'task/list', {'serialNumber': ''}))
    if not isinstance(data.get('taskList'), list):
        raise ReadStageError('活动没有返回完整浏览任务清单。')
    tasks = rows(data['taskList'])
    journal = Journal(context, 'vivo_appointment_browse')
    entries = []
    for task in tasks:
        if str(task.get('taskStatus')) == '1':
            continue
        if yes(task.get('isRemoteTask')) or str(task.get('taskType')) != '1' or (not task.get('taskLink')):
            entries.append(entry(target, 'action_required', '此任务需要真实互动、订单或其他官方前置', reason_kind='eligibility'))
            continue
        identifier = task.get('taskId')
        if not identifier:
            raise ReadStageError('浏览任务未返回真实任务编号。')
        rid = f"{target['id']}:{identifier}:{timezone.localdate()}"
        if journal.saved(rid).get('state') == 'pending':
            entries.append(entry(target, 'action_required', '此前浏览提交由后台回查，本条不重复提交', reason_kind='auto_review'))
            continue
        seconds = task.get('scanTime', task.get('browseTime', 5))
        if not str(seconds).isdigit() or not 0 <= int(seconds) <= 60:
            raise ReadStageError('浏览任务停留时间超出支持范围。')
        client.visit(task['taskLink'])
        until = time.monotonic() + max(1, int(seconds))
        while time.monotonic() < until:
            context.ensure_allowed()
            time.sleep(min(1, until - time.monotonic()))
        journal.submit(rid, lambda: apc(client, target, 'task/finish', {'taskId': identifier, 'appPkg': '', 'locationAuthorizedDTO': {'hasUserLocation': False, 'province': '', 'city': ''}}, writes=True), before=task.get('currentFinishCount'))
        after = obj(apc(client, target, 'task/list', {'serialNumber': ''}))
        current = next((t for t in rows(after.get('taskList')) if str(t.get('taskId')) == str(identifier)), {})
        if str(current.get('taskStatus')) == '1' or amount(current.get('currentFinishCount', 0)) > amount(task.get('currentFinishCount', 0)):
            journal.confirm(rid)
            entries.append(entry(target, 'success', '已实际浏览并达到停留时间，官方任务进度已回读'))
        else:
            entries.append(entry(target, 'action_required', '浏览完成已提交，等待官方进度更新，不重发', reason_kind='auto_review'))
        client.http.writing = False
    return entries or [entry(target, 'skipped', '当前没有未完成的免费浏览任务')]

def free_draw(api, client, context, target):
    msa = target['kind'] == 'reservation'
    if msa:
        state = shop_state(api, target)
        if str(state.get('lotteryActivityStatus')) != '1':
            return (entry(target, 'skipped', '本期预约抽奖未开放'), False)

        def available():
            return obj(api.call(SHOP, '/lottery/queryNumber', {'uuid': target['id']}, site=target['site']))

        def count(data):
            return amount(data['leftTimes'])

        def draw():
            return api.call(SHOP, '/lottery/prize', {'uuid': target['id'], **({'isAppointLottery': True} if target.get('launch') else {})}, site=target['site'], writes=True, method='POST')
    else:

        def available():
            return obj(apc(client, target, 'lottery/user/available/query'))

        def count(data):
            return amount(data['freeAvaCnt']) + amount(data['taskAvaCnt'])

        def draw():
            return apc(client, target, 'lottery/draw', {'topicId': target['site'], 'extend': {'hostUrl': 'https://zhan.vivo.com.cn/vivospace/' + target['site'] + '?h5JumpBackFlag=1', 'locationAuthorizedDTO': {'hasUserLocation': False, 'province': '', 'city': ''}}}, writes=True)
    before = available()
    try:
        total = count(before)
    except KeyError as error:
        raise ReadStageError('未取得明确免费抽奖次数，未提交抽奖。') from error
    if not total:
        return (entry(target, 'skipped', '当前没有已有免费抽奖次数'), False)
    for node in (before, state) if msa else (before,):
        if any((decimal_amount(node[name]) > 0 for name in ('pointsCost', 'cost', 'entryFee', 'price') if name in node)):
            return (entry(target, 'action_required', '此抽奖包含费用，未消耗积分', reason_kind='eligibility'), False)
    journal = Journal(context, 'vivo_appointment_draws' if msa else 'vivo_site_free')
    prefix = f"msa:{target['id']}:{timezone.localdate()}:" if msa else hashlib.sha256((target['site'] + target['id'] + str(timezone.localdate())).encode()).hexdigest() + ':'
    saved = obj(context.credentials.get('scopes', {}).get(journal.namespace))
    previous = [v for k, v in saved.items() if k.startswith(prefix)]
    if any((v.get('state') == 'pending' for v in previous)):
        return (entry(target, 'action_required', '上次抽奖已提交，后台回查原回执，不重复抽取', reason_kind='auto_review'), False)
    if len(previous) >= 30:
        return (entry(target, 'skipped', '本日抽奖已达到自动执行上限'), False)
    rid = prefix + str(len(previous))
    if msa:
        api.prepare_shop()
    receipt = obj(journal.submit(rid, draw, before={'count': total}))
    remaining = count(available())
    if remaining != total - 1:
        client.http.writing = False
        return (entry(target, 'action_required', '抽奖提交后的次数尚未确认，后台回查且不重发', reason_kind='auto_review'), False)
    journal.confirm(rid)
    client.http.writing = False
    prize = obj(receipt.get('data')) or receipt
    return (entry(target, 'success', '已使用1次免费机会；' + title(prize.get('rewardName') or prize.get('prizeName'), '抽奖结果已回读') + f'；剩余{remaining}次'), remaining > 0)

def comment_state(client, target):
    if target['kind'] == 'nps':

        def query(suffix):
            return client._call(WUKONG, '/wukongbcs/comment/' + suffix, params={'commentId': target['id'], 'siteId': target['site']}, writes=False, common=False, pkg='com.vivo.space', cookie_profile='wukong_appointment', referer='https://zhan.vivo.com.cn/vivospace/' + target['site']).get('data')
        detail = query('detail')
        submitted = query('has-comment')
        if submitted is True:
            query('submitted')
            return entry(target, 'success', '本人已提交的真实评价状态已核验，未重复提交')
        if not isinstance(detail, dict) or type(submitted) is not bool:
            raise ReadStageError('真实评价活动未返回明确参与状态。')
        return entry(target, 'skipped', '已核验当前尚无本人真实评价；不代写评分或评论', reason_kind='eligibility')
    nonce = hashlib.md5((client.device.imei + str(time.time_ns())).encode()).hexdigest()
    data = client._call(BBS, '/api/community/comment/queryComment', params={'lastId': '', 'tid': target['id'], 'pageNum': 1, 'pageSize': 20, 'order': 2, 'nonce': nonce}, method='POST', writes=False, pkg='com.vivo.space', with_vivo_account=True, common=False).get('data')
    if not isinstance(data, (dict, list)):
        raise ReadStageError('官方未返回评论活动数据。')
    return entry(target, 'success', '评论活动已查询；已有免费次数由抽奖任务自动使用，不发布虚构评论')

def execute(adapter, client, context, key, snapshot):
    cache = discover(client, context)
    if cache['todo']:
        return StepResult('skipped', f"正在核对当前活动入口，剩余 {len(cache['todo'])} 个；随后自动执行本项", snapshot, continue_after=1)
    api, entries, again = (AppointmentAPI(client), [], False)
    targets = cache['targets']
    cursor_key = '_taskflow_vivo_comment_cursor'
    cursor, fingerprint = (0, '')
    if key == 'appointment_comments':
        targets = [t for t in targets if t['kind'] in ('comment', 'nps')]
        fingerprint = hashlib.sha256(str([(t['site'], t['kind'], t['id']) for t in targets]).encode()).hexdigest()
        saved = obj(context.credentials.get('scopes', {}).get(cursor_key))
        if saved.get('date') == timezone.localdate().isoformat() and saved.get('fingerprint') == fingerprint:
            cursor = min(len(targets), max(0, int(saved.get('index', 0))))
        again = cursor + 8 < len(targets)
        targets = targets[cursor:cursor + 8]
    for target in targets:
        kind = target['kind']
        try:
            if key in ('appointment_sign', 'appointment_reminder') and kind == 'sign':
                entries.append(sign_or_remind(client, context, target, key))
            elif key == 'appointment_reserve' and kind in ('reservation', 'reserve_only'):
                entries.append(reserve(api, client, context, target))
            elif key == 'appointment_catalog':
                if kind == 'sign':
                    state = sign_state(client, target)
                    message = f"签到进度 {state.get('signDays')}/{state.get('cycle')}，" + ('今日已签' if state['todaySignFlag'] else '今日可签到')
                elif kind == 'reservation':
                    state = shop_state(api, target)
                    left = obj(api.call(SHOP, '/lottery/queryNumber', {'uuid': target['id']}, site=target['site']))
                    message = f"预约活动状态 {state.get('lotteryActivityStatus')}；剩余免费次数 {left.get('leftTimes', '未返回')}"
                else:
                    message = '官方当前页面已发布本活动入口'
                entries.append(entry(target, 'success', message))
            elif key == 'appointment_browse' and kind in ('sign', 'lottery'):
                entries.extend(browse(client, context, target))
            elif key == 'appointment_draw' and kind in ('reservation', 'lottery'):
                item, more = free_draw(api, client, context, target)
                entries.append(item)
                again = again or more
            elif key == 'appointment_comments' and kind in ('comment', 'nps'):
                entries.append(comment_state(client, target))
        except (ActionRequired, ReadStageError) as error:
            client.http.writing = False
            entries.append(entry(target, 'action_required', error.message, **error.diagnostics))
        except UnknownWriteResult:
            client.http.writing = False
            entries.append(entry(target, 'action_required', '本条提交回执尚未确认，后台回查且不重发，其他活动继续', reason_kind='auto_review'))
    if key == 'appointment_comments':
        scopes = deepcopy(context.credentials.get('scopes', {}))
        scopes[cursor_key] = {'date': timezone.localdate().isoformat(), 'fingerprint': fingerprint, 'index': cursor + len(targets) if again else 0}
        context.persist_scopes(scopes)
    current = adapter.profile(client).snapshot if key in ('appointment_sign', 'appointment_draw', 'appointment_browse') else {}
    if 'points' in current and current['points'] < snapshot.get('points', current['points']):
        raise UnknownWriteResult('免费活动后的积分变化未通过核验，已停止本项后续提交。')
    snapshot = {**snapshot, **current}
    done = sum((e['status'] == 'success' for e in entries))
    skipped = sum((e['status'] == 'skipped' for e in entries))
    pending = len(entries) - done - skipped
    message = f'已查询 {len(entries)} 类当前活动入口；查询不计为任务收益' if key == 'appointment_catalog' else f'已核验评论与评价 {len(entries)} 项；未发布虚构内容' if key == 'appointment_comments' else f'执行确认 {done} 项，已完成或暂无可执行 {skipped} 项，条件待满足 {pending} 项'
    outcome = report(snapshot, entries, message)
    return StepResult(outcome.status, outcome.message, snapshot, diagnostics=outcome.diagnostics, continue_after=1 if again else None)
