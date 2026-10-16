# Bundled vivo protocol reference; see source-manifest.json.
"""Current task-center catalogs for every actual vivo client package."""
import re
from copy import deepcopy
from .compat import timezone
from .added_common import obj, report, rows
from .protocol_common import ActionRequired, ProtocolError
from .sdk import CredentialsExpired, StepResult, TaskSpec, UnknownWriteResult
from .vendor.vivo.taskcenter import BIZ_PACKAGE, POINTH5
CLIENT_NAMES = {'appstore': '应用商店', 'gamecenter': '游戏中心', 'vivospace': '官网 App', 'browser': '浏览器', 'videoPlayer': '视频', 'wallet': '钱包', 'iTheme': '主题', 'iMusic': '音乐', 'minigame': '小游戏', 'quickapp': '快应用', 'vhome': '智慧生活'}
TASKS = tuple((TaskSpec('client_tasks_' + biz, name + '全分类任务执行', True) for biz, name in CLIENT_NAMES.items()))

def remember_sites(context, biz, tasks):
    found = sorted({site for task in tasks for field in ('jumpURL', 'jumpUrl', 'url', 'taskUrl') for site in re.findall('wk[0-9a-z]{10,16}', str(task.get(field) or ''))})
    scopes = deepcopy(context.credentials.get('scopes', {}))
    cached = obj(scopes.get('_taskflow_vivo_client_sites'))
    if cached.get('date') != timezone.localdate().isoformat():
        cached = {'date': timezone.localdate().isoformat(), 'packages': {}}
    known = set((v for values in obj(cached.get('packages')).values() for v in values))
    cached.setdefault('packages', {})[biz] = found
    scopes['_taskflow_vivo_client_sites'] = cached
    if set(found) - known:
        scopes.pop('_taskflow_vivo_appointment_catalog', None)
    context.persist_scopes(scopes)

def catalog(client, biz):
    package = BIZ_PACKAGE[biz]
    client.pkg = package
    value = client._call(POINTH5, '/api/task/list.do', method='POST', params={}, pkg=package, writes=False)
    groups = obj(value.get('data')).get('tasks')
    if not isinstance(groups, list):
        raise ProtocolError('当前客户端没有返回完整的分组任务目录。')
    found = {}
    for group in rows(groups):
        if not isinstance(group.get('list'), list):
            raise ProtocolError('当前客户端任务分组缺少实际清单。')
        items = client.group_list(str(group['type'])) if group.get('hasMore') is True and group.get('type') is not None else group['list']
        for task in rows(items):
            if task.get('id') is None:
                raise ProtocolError('官方任务缺少任务编号，未猜测对象。')
            found[str(task['id'])] = task
    return list(found.values())

def pending_elsewhere(context, task_id, own_key):
    journal = obj(obj(context.credentials.get('scopes')).get('_taskflow_vivo_tasks'))
    for key, value in journal.items():
        if key.startswith(own_key + ':') or value.get('state') != 'pending':
            continue
        if key.endswith((':web:' + task_id, ':reward:' + task_id, ':native:' + task_id)):
            return True
    return False

def real_behavior(adapter, client, context, biz, task, snapshot):
    name = str(task.get('name') or '')
    if biz == 'vivospace':
        from .vivo_shop_actions import execute_one, kind_for
        kind = kind_for(task)
        if kind:
            return execute_one(adapter, client, context, task, kind, snapshot, lambda: catalog(client, biz), 'client_tasks_vivospace')
    if biz == 'browser':
        from .vivo_browser_tasks import execute_one, kind_for
        kind = kind_for(task)
        if kind:
            return execute_one(adapter, client, context, task, kind, snapshot, lambda: catalog(client, biz), 'client_tasks_browser')
    if biz == 'vivospace' and ('阅读' in name and '帖子' in name or ('收藏' in name and '商品' in name)):
        from .vivo_native_earn import execute as native_execute
        return native_execute(adapter, client, context, 'community_reads' if '阅读' in name else 'shop_favorite', snapshot)
    if biz == 'browser' and '阅读' in name and any((word in name for word in ('新闻', '资讯'))):
        from .vivo_news import execute as news_execute
        return news_execute(adapter, client, context, snapshot)
    if biz == 'gamecenter' and '预约' in name:
        from .vivo_game_reservation import execute as reserve_execute
        return reserve_execute(adapter, client, context, snapshot)
    return None

def execute(adapter, client, context, key, snapshot):
    biz = key.removeprefix('client_tasks_')
    if biz not in CLIENT_NAMES:
        raise ProtocolError('未注册的 vivo 客户端任务。')
    previous = client.pkg
    entries = []
    more = False
    processed = 0
    try:
        tasks = catalog(client, biz)
        remember_sites(context, biz, tasks)
        for task in tasks:
            context.ensure_allowed()
            identifier = str(task['id'])
            if pending_elsewhere(context, identifier, key):
                entries.append({'name': str(task.get('name') or '客户端任务'), 'status': 'action_required', 'message': '其他入口保留了此任务的未确认提交；只隔离该项，其他任务继续。', 'reason_kind': 'settlement'})
                continue
            if processed >= 4:
                more = True
                break
            initial = [task]

            def load(tid=identifier, initial=initial):
                if initial:
                    return [initial.pop()]
                return [t for t in catalog(client, biz) if str(t['id']) == tid]
            try:
                if not task.get('taskComplete') and (not task.get('unreceviedPoints')):
                    behavior = real_behavior(adapter, client, context, biz, task, snapshot)
                    if behavior is not None:
                        snapshot = behavior.snapshot
                        entries.extend(behavior.diagnostics.get('entries', []))
                        more = more or bool(behavior.continue_after)
                        if behavior.diagnostics.get('skip_kind') == 'native_unavailable' or behavior.diagnostics.get('native_evidence'):
                            continue
                        client.pkg = BIZ_PACKAGE[biz]
                        initial.clear()
                result = adapter.task_group(client, context, key, snapshot, load=load, refresh_profile=False)
                snapshot = result.snapshot
                entries.extend(result.diagnostics.get('entries', []))
                if result.status == 'success':
                    processed += 1
                more = more or bool(result.continue_after)
            except ActionRequired as error:
                client.http.writing = False
                entries.append({'name': str(task.get('name') or '客户端任务'), 'status': 'action_required', 'message': error.message, **error.diagnostics})
            except UnknownWriteResult:
                client.http.writing = False
                entries.append({'name': str(task.get('name') or '客户端任务'), 'status': 'action_required', 'message': '本项原提交结果尚未确认，保留原意图且不重发；其他客户端任务继续。', 'reason_kind': 'settlement'})
            except ProtocolError as error:
                client.http.writing = False
                entries.append({'name': str(task.get('name') or '客户端任务'), 'status': 'failed', 'message': error.message, **error.diagnostics})
    except CredentialsExpired as error:
        raise ActionRequired('本客户端目录会话当前不可用，本项跳过；其他可用业务端继续。', reason_kind='session_unavailable') from error
    finally:
        client.pkg = previous
    result = report(snapshot, entries, CLIENT_NAMES[biz] + '当前全分类任务已逐项执行与核验')
    return StepResult(result.status, result.message, result.snapshot, diagnostics={**result.diagnostics, 'client_package': BIZ_PACKAGE[biz], 'catalog_task_count': len(tasks)}, continue_after=1 if more else None)
