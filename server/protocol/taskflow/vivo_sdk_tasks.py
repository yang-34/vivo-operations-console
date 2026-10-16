# Bundled vivo protocol reference; see source-manifest.json.
"""Legacy SDK entries now dispatch actual client actions and official readback."""
import re
from .compat import timezone
from .added_common import Journal, obj, report, title
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, TaskSpec
from .vendor.vivo.taskcenter import BIZ_PACKAGE, POINTSDK
NAMES = dict(zip(BIZ_PACKAGE, ('应用商店', '游戏中心', '官网 App', '浏览器', '视频', '钱包', '主题', '音乐', '小游戏', '快应用', '智慧生活', 'vivo+'), strict=True))
TASKS = tuple((TaskSpec('sdk_' + biz, name + '任务兼容入口', True, False, '已合并到' + name + '全分类任务执行；保留历史记录和手动兼容入口，后台不重复调度。') for biz, name in NAMES.items() if BIZ_PACKAGE[biz]))
CONFIG = '/sdk/config/action/list'
UPLOAD = '/sdk/action/upload'
RECEIVE = '/sdk/task/receive/point'

def rejected_outcome(entry):
    code = str(entry.get('provider_code', ''))
    messages = {'1001': '当前 SDK 行为没有待领任务；未将上报回执计为任务完成', '10002': '官方确认该行为今日已领取，不重复领取', '10005': '当前 SDK 行为与可领奖任务属性不匹配，没有本项可领奖励'}
    if code in messages:
        return {**entry, 'status': 'skipped', 'message': messages[code], 'skip_kind': 'already_done' if code == '10002' else 'no_reward', 'provider_result_status': 'skipped', 'reason_kind': 'normal'}
    return {**entry, 'status': 'failed', 'reason_kind': 'provider_rejected', 'provider_result_status': 'failed'}

def actions(client, biz):
    pkg = BIZ_PACKAGE[biz]
    try:
        data = client._call(POINTSDK, CONFIG, method='POST', params={'pkgName': pkg}, pkg=pkg, common=False, writes=False).get('data')
    except ActionRequired as error:
        raise ProtocolError('当前 SDK 配置请求被拒绝，不能记为账号条件不足。', provider_code=error.diagnostics.get('provider_code', ''), reason_kind='provider_rejected') from error
    if not isinstance(data, dict) or not isinstance(data.get('actions'), list):
        raise ProtocolError('积分 SDK 没有返回完整的当前行为定义。')
    if len(data['actions']) > 256:
        raise ProtocolError('当前业务的 SDK 配置超过单轮支持范围。')
    result, seen = ([], set())
    for row in data['actions']:
        if not isinstance(row, dict) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', str(row.get('actionId', ''))):
            raise ProtocolError('积分 SDK 当前行为标识不完整，未猜测编号。')
        aid = str(row['actionId'])
        if aid not in seen:
            seen.add(aid)
            result.append(aid)
    return result

def counts(tasks):
    return {str(t['id']): {'count': amount(t.get('completedTimes') or 0), 'name': title(t.get('name')), 'unclaimed': amount(t.get('unreceviedPoints') or 0)} for t in tasks if t.get('id') is not None}

def execute(adapter, client, context, key, snapshot):
    """Compatibility entry for real client work; never invent SDK counters."""
    from .vivo_client_tasks import execute as client_execute
    biz = key.removeprefix('sdk_')
    if biz not in BIZ_PACKAGE or not BIZ_PACKAGE[biz]:
        raise ProtocolError('没有对应的 SDK 业务。')
    result = client_execute(adapter, client, context, 'client_tasks_' + biz, snapshot)
    entries = list(result.diagnostics.get('entries', []))
    journal = Journal(context, 'vivo_sdk_' + biz)
    today = timezone.localdate().isoformat()
    history = obj(context.credentials.get('scopes', {}).get(journal.namespace))
    configured = sorted({k.rsplit(':', 1)[0] for k in history if re.fullmatch('\\d{4}-\\d{2}-\\d{2}:[A-Za-z0-9_-]{1,64}:(?:upload|receive)', k)})
    unresolved = 0
    for rid in configured:
        aid = rid.split(':', 1)[1]
        settled = journal.saved(rid + ':receive')
        if rid.startswith(today + ':') and settled.get('state') == 'confirmed':
            entries.append({'name': NAMES[biz] + ' SDK ' + aid, 'status': 'skipped', 'message': '本行为今日已有历史核验记录，未重复提交，不计为本轮新增收益。', 'skip_kind': 'already_done'})
        if any((journal.saved(rid + ':' + stage).get('state') == 'pending' for stage in ('upload', 'receive'))):
            unresolved += 1
            entries.append({'name': NAMES[biz] + ' SDK ' + aid, 'status': 'action_required', 'message': '保留历史未确认提交供后台自动核对；未重发次数或领取，不阻塞其他任务。', 'reason_kind': 'settlement'})
    tasks = client.ec_list(biz)
    pending_ids = {str(t['id']) for t in tasks if t.get('id') is not None and amount(t.get('unreceviedPoints') or 0) > 0}
    snapshot = result.snapshot
    more = bool(result.continue_after)
    if pending_ids:
        awarded = adapter.task_group(client, context, 'ec_' + biz, snapshot, load=lambda: [t for t in client.ec_list(biz) if str(t.get('id')) in pending_ids])
        snapshot = awarded.snapshot
        entries.extend(awarded.diagnostics.get('entries', []))
        more |= bool(awarded.continue_after)
    combined = report(snapshot, entries, NAMES[biz] + '已执行实际客户端任务并回读；未虚报SDK次数')
    return StepResult(combined.status, combined.message, snapshot, diagnostics={**combined.diagnostics, 'legacy_actions': len(configured), 'synthetic_uploads': 0, 'legacy_unconfirmed': unresolved}, continue_after=1 if more else None)
