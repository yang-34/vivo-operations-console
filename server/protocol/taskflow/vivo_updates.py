# Bundled vivo protocol reference; see source-manifest.json.
"""Dynamic site rewards and current owned membership/signing entitlements."""
import hashlib
import json
import re
from urllib.parse import unquote
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, ReadStageError, amount, decimal_amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
from .vivo_activities import POINT, WUKONG, Activities, walk
TASKS = (TaskSpec('current_sites', '发现全部当期悟空站点与活动'), TaskSpec('site_free_draws', '使用各站点已有免费抽奖机会', True), TaskSpec('consecutive_rewards', '领取已达标连续签到奖励', True), TaskSpec('phone_rewards', '领取当前任务已达标话费奖励', True), TaskSpec('member_applet_benefits', '领取会员小程序新增免费权益', True), TaskSpec('member_growth', '查询会员成长值与等级'), TaskSpec('store_task_catalog', '查询当期门店任务与本人进度'))

def sites(client):
    sources = [client._call(POINT, '/api/mall/task/center/new/signBizList.do', pkg='com.vivo.space').get('data')]
    for path in ('/home/page/v5/bannerInfo.do', '/homepage/commonInfo.do'):
        sources.append(client.member(path, form=True).get('data'))
    for path in ('/api/nmall/home/page/head.do', '/api/nmall/home/activity/zone.do'):
        sources.append(client._call(POINT, path).get('data'))
    from .sdk import PlatformError
    from .vivo_native_earn import Native
    try:
        sources.append(Native(client).eden('/v3/home/index'))
    except PlatformError:
        pass
    current = obj(obj(client.credentials.get('scopes')).get('_taskflow_vivo_client_sites'))
    if current.get('date') == timezone.localdate().isoformat():
        sources.append(current.get('packages', {}))
    return sorted(set(re.findall('wk[0-9a-z]{10,16}', json.dumps(sources, ensure_ascii=False))))

def site_config(api, site):
    state = obj(api.call(WUKONG, '/wukongtopic/site/online/query', {'siteId': site, 'noTimestamp': True}, site=site))
    if str(state.get('state')) in ('4', '6'):
        return None
    raw = state.get('config')
    if raw is None:
        raw = api.call(WUKONG, '/wukongtopic/site/config/query', {'siteId': site, 'noTimestamp': True}, site=site)
    if isinstance(raw, str):
        try:
            raw = json.loads(unquote(raw))
        except ValueError as exc:
            raise ProtocolError('当前站点配置格式未识别。') from exc
    if not isinstance(raw, (dict, list)):
        raise ActionRequired('该站点当前没有完整活动配置。', reason_kind='unavailable')
    return raw

def free_cost(data):
    return all((decimal_amount(data.get(k, 0)) == 0 for k in ('pointsCost', 'cost', 'price', 'entryFee')))

def execute(adapter, client, context, key, snapshot):
    if key == 'site_free_draws':
        from .vivo_appointments import execute as published_draws
        return published_draws(adapter, client, context, 'appointment_draw', snapshot)
    if key == 'current_sites':
        return site_rewards(adapter, client, context, key, snapshot)
    if key == 'member_applet_benefits':
        return applet_benefits(adapter, client, context, snapshot)
    if key == 'member_growth':
        value = obj(client.member('/applet/xp/userInfo.do').get('data'))
        member = obj(value.get('memberInfo')) or value
        xp = member.get('xp', member.get('experience'))
        if xp is None:
            raise ProtocolError('会员中心没有返回明确成长值。')
        try:
            client.member('/applet/xp/listInfo.do', method='GET')
            detail = '明细已查询'
        except ActionRequired as exc:
            detail = '成长值已回读，明细查询返回' + str(exc.diagnostics.get('provider_code', '未知状态'))
        return StepResult('success', f"会员成长值{amount(xp)}，等级{title(member.get('levelName'), '未发布')}；" + detail, snapshot)
    if key == 'store_task_catalog':
        data = client._call(POINT, '/api/store/task/list.do', pkg='com.vivo.space').get('data')
        tasks = [] if data == {} else data if isinstance(data, list) else obj(data).get('tasks')
        if not isinstance(tasks, list):
            raise ProtocolError('平台未返回完整门店任务清单。')
        entries = [{'name': title(t.get('name')), 'status': 'skipped' if amount(t.get('completedTimes', 0)) >= amount(t.get('totalTime', 1)) else 'action_required', 'message': '本人门店任务进度已查询；领取需实际到店核销'} for t in rows(tasks)]
        result = report(snapshot, entries, '当前门店任务已核对')
        return StepResult('success', result.message, snapshot, diagnostics=result.diagnostics)
    if key == 'consecutive_rewards':
        from .vivo import SIGNHOST

        def load():
            try:
                return obj(client._call(SIGNHOST, '/api/sign/consecutive/info.do', pkg='com.vivo.wallet').get('data'))
            except ReadStageError as exc:
                if exc.diagnostics.get('provider_code') == 'HTTP_404':
                    raise ActionRequired('当前连签活动入口未发布，等待官方恢复。', reason_kind='unavailable', provider_code='HTTP_404') from exc
                raise

        def ready(data):
            return [n for n in walk(data) if n.get('signAwardOrderNo') and any((yes(n.get(k)) for k in ('canReceive', 'canClaim', 'receiveable')))]
        return grant_rows(adapter, client, context, snapshot, key, load, ready, lambda row: client._call(SIGNHOST, '/api/sign/consecutive/receive.do', method='POST', params={'signAwardOrderNo': row['signAwardOrderNo']}, pkg='com.vivo.wallet'), 'signAwardOrderNo')
    if key == 'phone_rewards':

        def load():
            return obj(client._call(POINT, '/api/mall/task/center/tasklist.do', params={'type': 1, 'businessCode': ''}).get('data'))

        def ready(data):
            return [t for t in rows(data.get('tasks')) if ('话费' in str(t.get('name', '')) or t.get('rewardType') == 'PHONE') and yes(t.get('canReceive')) and (t.get('id') is not None)]
        return grant_rows(adapter, client, context, snapshot, key, load, ready, lambda row: client._call(POINT, '/api/task/getPhoneReward.do', method='POST', params={}, pkg='com.vivo.space'), 'id')
    raise ProtocolError('没有对应的新增vivo流程。')

def grant_rows(adapter, client, context, snapshot, key, load, ready, submit, field):
    journal, entries, completed, more = (Journal(context, 'vivo_new_grants'), [], 0, False)
    for row in ready(load()):
        if not free_cost(row):
            entries.append({'name': title(row.get('name')), 'status': 'action_required', 'message': '本项包含费用或积分消耗，需逐笔确认'})
            continue
        rid = f'{key}:{row[field]}:{timezone.localdate()}'
        if journal.saved(rid).get('state') == 'confirmed':
            continue
        if completed >= 3:
            more = True
            break
        journal.submit(rid, lambda: submit(row), before=row)
        if any((str(n.get(field)) == str(row[field]) for n in ready(load()))):
            raise UnknownWriteResult
        current = adapter.profile(client).snapshot
        if current['points'] < snapshot['points']:
            raise UnknownWriteResult
        snapshot = {**snapshot, **current}
        journal.confirm(rid)
        client.http.writing = False
        entries.append({'name': title(row.get('name'), '当前达标奖励'), 'status': 'success', 'message': '本人待领状态已清除，积分未扣减'})
        completed += 1
    result = report(snapshot, entries, '当前新增奖励已回查')
    return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)

def applet_benefits(adapter, client, context, snapshot):

    def load():
        data = obj(client.member('/applet/benefit/mynew.do', method='GET').get('data'))
        if set(data) == {'level'} and (data['level'] is None or isinstance(data['level'], int)):
            return {'newBenefitList': []}
        if not isinstance(data.get('newBenefitList'), list):
            raise ProtocolError('会员小程序新增权益清单不完整。')
        return data

    def ready(data):
        return [dict(n, benefitId=n.get('benefitId') or n.get('id')) for n in rows(data.get('newBenefitList')) if (n.get('benefitId') or n.get('id')) and n.get('userStatus', 0) == 0]
    return grant_rows(adapter, client, context, snapshot, 'member_applet_benefits', load, ready, lambda row: client.member('/applet/benefit/receiveByRisk.do', body={'benefitId': row['benefitId'], 'version': '3.8'}, writes=True), 'benefitId')

def site_rewards(adapter, client, context, key, snapshot):
    api, entries, completed, more = (Activities(client), [], 0, False)
    journal = Journal(context, 'vivo_site_free')
    for site in sites(client):
        try:
            config = site_config(api, site)
        except ActionRequired as exc:
            entries.append({'name': site, 'status': 'action_required', 'message': exc.message})
            continue
        if config is None:
            entries.append({'name': site, 'status': 'skipped', 'message': '站点当前已下线'})
            continue
        identifiers = sorted({str(n['activityId']) for n in walk(config) if n.get('activityId')})
        if key == 'current_sites':
            entries.append({'name': site, 'status': 'success', 'message': f'在线配置已查询，发布{len(identifiers)}个活动'})
            continue
        for aid in identifiers:
            for group in (0, 1):
                params = {'activityId': aid, 'siteId': site, **({'groupId': group} if group else {})}

                def available():
                    return obj(api.call(WUKONG, '/wukongapc/lottery/user/available/query', params, site=site))
                try:
                    before = available()
                except ActionRequired as exc:
                    if exc.diagnostics.get('provider_code') in ('1000', '14001'):
                        continue
                    entries.append({'name': site, 'status': 'action_required', 'message': '该活动的抽奖机会查询未被接受，其他场次继续核验', 'provider_code': exc.diagnostics.get('provider_code', '')})
                    continue
                count = amount(before.get('freeAvaCnt', 0)) + amount(before.get('taskAvaCnt', 0))
                if not count:
                    continue
                if not free_cost(before):
                    entries.append({'name': site, 'status': 'action_required', 'message': '当前抽奖附带积分费用，未自动参加'})
                    break
                if completed >= 3:
                    more = True
                    break
                prefix = hashlib.sha256((site + aid + str(timezone.localdate())).encode()).hexdigest()
                saved = obj(context.credentials.get('scopes', {}).get(journal.namespace))
                attempts = [v for k, v in saved.items() if k.startswith(prefix)]
                if any((v.get('state') == 'pending' for v in attempts)):
                    raise ActionRequired('该站点此前抽取结果待核验，未重复提交。', reason_kind='settlement')
                if len(attempts) >= 30:
                    entries.append({'name': site, 'status': 'action_required', 'message': '本日已核验30次，暂不继续增加请求'})
                    break
                rid = prefix + ':' + str(len(attempts))
                receipt = journal.submit(rid, lambda: api.call(WUKONG, '/wukongapc/lottery/draw', {**params, 'topicId': site, 'extend': {'hostUrl': 'https://zhan.vivo.com.cn/point/' + site + '?h5JumpBackFlag=1', 'locationAuthorizedDTO': {'hasUserLocation': False, 'province': '', 'city': ''}}}, site=site, method='POST', writes=True), before={'points': snapshot['points'], 'count': count})
                after = available()
                remaining = amount(after.get('freeAvaCnt', 0)) + amount(after.get('taskAvaCnt', 0))
                current = adapter.profile(client).snapshot
                if remaining != count - 1 or current['points'] < snapshot['points'] or (not isinstance(receipt, dict)):
                    raise UnknownWriteResult
                snapshot = {**snapshot, **current}
                journal.confirm(rid)
                client.http.writing = False
                completed += 1
                more = more or remaining > 0
                entries.append({'name': site, 'status': 'success', 'message': '已使用1次已有免费机会，结果：' + title(receipt.get('rewardName'), '结果已回读') + f'；剩余{remaining}次'})
                break
            if more and completed >= 3:
                break
        if more and completed >= 3:
            break
    result = report(snapshot, entries, '全部当期站点及免费权益已核对')
    return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
