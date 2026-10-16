# Bundled vivo protocol reference; see source-manifest.json.
"""Actual visits, remote eligibility settlement and existing activity rewards."""
import json
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, UnknownWriteResult
from .vivo_activities import walk

def execute(adapter, client, context, api, key, snapshot):
    if key in ('weekly_tasks', 'golden_tasks', 'monopoly_tasks'):
        return visit_tasks(client, context, api, key, snapshot)
    if key == 'double11_remind':
        data = coupon_state(api)
        count = len(rows(data.get('couponGrabTabList')))
        return StepResult('success', f'已查询{count}个领券场次；提醒开关可在独立确认模块设置', snapshot)
    journal = Journal(context, 'vivo_activity_rewards')
    if key == 'quiz_rewards':
        info = obj(api.quiz('user/info'))
        if not yes(info.get('openRewardFlag')):
            return StepResult('skipped', '当前答题榜单领奖未开放', snapshot)
        api.quiz('rank/list')
        api.quiz('rank/prize/list')
        rid = 'quiz:' + api.config('quiz')['activityId']
        journal.submit(rid, lambda: api.quiz('rank/gift/receive', writes=True))
        after = adapter.profile(client).snapshot
        if after['points'] <= snapshot['points']:
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        return StepResult('success', '答题榜单奖励到账已回读', {**snapshot, **after})
    if key == 'monopoly_rewards':
        state = api.monopoly('user/info')
        pending = [n for n in walk(state) if n.get('topicId') and yes(n.get('canReceive'))]
        if not pending:
            return StepResult('skipped', '当前没有已达标待领的大富翁奖励', snapshot)
        entries = []
        for row in pending[:3]:
            rid = 'monopoly:' + str(row['topicId'])
            journal.submit(rid, lambda: api.monopoly_write('carveUp', {'topicId': row['topicId']}))
            after = api.monopoly('user/info')
            if any((n.get('topicId') == row['topicId'] and yes(n.get('canReceive')) for n in walk(after))):
                raise UnknownWriteResult
            journal.confirm(rid)
            client.http.writing = False
            entries.append({'name': '大富翁本期奖励', 'status': 'success', 'message': '已领取状态回读通过'})
        result = report(snapshot, entries)
        return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if len(pending) > 3 else None)
    if key == 'koicarp_rewards':
        state = obj(api.koi('init'))
        info, user = (obj(state.get('koicarpActivityInfo')), obj(state.get('userInfo')))
        if amount(user.get('koicarpNum', 0)) == 0:
            return StepResult('skipped', '本人当前没有锦鲤获奖记录', snapshot)
        entries = []
        for point in info.get('points', []):
            value = api.koi('schedule', {'point': point, 'version': info.get('version'), 'sessionNum': 0})
            for row in walk(value):
                if row.get('sessionNum') is None or not yes(row.get('canReceive')):
                    continue
                params = {'point': point, 'version': info['version'], 'sessionNum': row['sessionNum'], 'ticket': '', 'scene': 'koicarp', 'constId': ''}
                rid = 'koi:' + str(point) + ':' + str(row['sessionNum'])
                journal.submit(rid, lambda: api.koi('receive', params, writes=True))
                after = api.koi('schedule', {'point': point, 'version': info['version'], 'sessionNum': row['sessionNum']})
                if any((n.get('sessionNum') == row['sessionNum'] and yes(n.get('canReceive')) for n in walk(after))):
                    raise UnknownWriteResult
                journal.confirm(rid)
                client.http.writing = False
                entries.append({'name': '锦鲤已开奖奖励', 'status': 'success', 'message': '领取状态已回读'})
        return report(snapshot, entries, '锦鲤本人已开奖场次已核对')
    raise ProtocolError('独立玩法任务没有注册执行路径。')

def coupon_state(api):
    config = api.config('double11')['config']
    ids = {str(n['activityId']) for n in walk(config) if n.get('activityId') and 'couponStyle' in n}
    if len(ids) != 1:
        raise ActionRequired('当前双11配置未提供唯一领券活动标识。', reason_kind='unavailable')
    aid = ids.pop()
    data = obj(api.call('https://mshopact.vivo.com.cn', '/couponGrab/queryActivity', {'activityId': aid, 'style': 1, 'tabId': ''}))
    data['_activityId'] = aid
    return data

def visit_tasks(client, context, api, key, snapshot):
    family = {'weekly_tasks': 'weekly', 'golden_tasks': 'golden', 'monopoly_tasks': 'monopoly'}[key]
    client.http.hosts |= {'scrmact.vivo.com.cn', 'mall.vivo.com.cn', 'shop.vivo.com.cn'}
    journal = Journal(context, 'vivo_activity_visits')

    def read():
        data = api.monopoly('task/list') if family == 'monopoly' else api.w(family, '/wukongapc/task/list', {'serialNumber': ''})
        source = data if isinstance(data, list) else obj(data).get('taskList')
        if not isinstance(source, list):
            raise ProtocolError('当前玩法未返回完整任务表。')
        return rows(source)
    tasks = read()
    if family == 'weekly' and any((yes(t.get('isRemoteTask')) and t.get('taskStatus') != 1 for t in tasks)):
        api.w(family, '/wukongapc/task/remote/finish', {'locationAuthorizedDTO': json.dumps({'hasUserLocation': False, 'province': '', 'city': ''}, separators=(',', ':'))}, writes=True)
        tasks = read()
        client.http.writing = False
    entries, count, more = ([], 0, False)
    for task in tasks:
        name = title(task.get('taskName'))
        if str(task.get('taskStatus')) == '1':
            entries.append({'name': name, 'status': 'skipped', 'message': '平台确认已完成'})
            continue
        if yes(task.get('isRemoteTask')) or str(task.get('taskType')) != '1' or (not task.get('taskLink')):
            entries.append({'name': name, 'status': 'action_required', 'message': '需本人完成真实关系、支付或其他官方前置；已查询当前进度'})
            continue
        if count >= 3:
            more = True
            break
        tid = task.get('taskId')
        if not tid:
            raise ProtocolError('当前任务没有服务器 taskId。')
        client.visit(task['taskLink'])
        rid = family + ':' + str(tid) + ':' + timezone.localdate().isoformat()

        def finish():
            if family == 'monopoly':
                return api.monopoly_write('task/finish', {'taskId': tid, 'appPkg': ''})
            return api.w(family, '/wukongapc/task/finish', {'taskId': tid, 'appPkg': '', 'locationAuthorizedDTO': {'hasUserLocation': False, 'province': '', 'city': ''}}, writes=True, method='POST')
        journal.submit(rid, finish, before=task.get('currentFinishCount'))
        after = next((t for t in read() if t.get('taskId') == tid), None)
        if not after or not (str(after.get('taskStatus')) == '1' or amount(after.get('currentFinishCount')) > amount(task.get('currentFinishCount'))):
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        count += 1
        more = more or str(after.get('taskStatus')) != '1'
        entries.append({'name': name, 'status': 'success', 'message': '已实际访问页面并核验任务计数'})
    result = report(snapshot, entries, '本期玩法任务已逐项执行与回读')
    return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
