# Bundled vivo protocol reference; see source-manifest.json.
"""Published sign businesses, including walletWelfare and future replacements."""
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError
from .sdk import CredentialsExpired, StepResult, TaskSpec, UnknownWriteResult
from .vendor.vivo.taskcenter import BIZ_PACKAGE, POINTH5, SIGNHOST
TASKS = (TaskSpec('published_business_signs', '执行官方当期全部业务签到', True),)

def wallet_replacement(adapter, client, context, snapshot, name):
    from .vivo_native_updates import execute as native_execute
    try:
        result = native_execute(adapter, client, context, 'wallet_native_sign', snapshot)
        return (result.snapshot, {'name': name, 'status': result.status, 'message': result.message, 'replacement_task': 'wallet_native_sign', **result.diagnostics})
    except ActionRequired as error:
        return (snapshot, {'name': name, 'status': 'action_required', 'message': error.message, 'replacement_task': 'wallet_native_sign', **error.diagnostics})

def execute(adapter, client, context, key, snapshot):
    published = client._call(POINTH5, '/api/mall/task/center/new/signBizList.do', pkg='com.vivo.space', writes=False).get('data')
    if not isinstance(published, list):
        raise ProtocolError('官方没有返回完整的当期签到业务清单。')
    journal = Journal(context, 'vivo_published_signs')
    entries = []
    processed = 0
    more = False
    for row in rows(published):
        code = str(row.get('bizCode') or '')
        package = row.get('packageName') or BIZ_PACKAGE.get(code)
        name = title(row.get('bizName'), '业务签到')
        if yes(row.get('isSign')):
            entries.append({'name': name, 'status': 'skipped', 'message': '官方当期业务清单确认今日已签'})
            continue
        if not code or package not in set(BIZ_PACKAGE.values()) - {''}:
            entries.append({'name': name, 'status': 'action_required', 'message': '新业务没有下发可确认的客户端包名，未猜测签到身份。', 'reason_kind': 'missing_context'})
            continue
        if processed >= 3:
            more = True
            break
        rid = code + ':' + timezone.localdate().isoformat()

        def read():
            data = obj(client._call(SIGNHOST, '/api/sign/info.do', params={'sysCode': code}, pkg=package, writes=False).get('data'))
            if 'todayIsSign' not in data:
                raise ProtocolError('当前业务未返回明确今日签到状态。')
            return data
        try:
            before = read()
            if yes(before['todayIsSign']):
                if journal.saved(rid).get('state') == 'pending':
                    journal.confirm(rid)
                entries.append({'name': name, 'status': 'skipped', 'message': '官方签到页确认今日已签，未重复提交'})
                continue
            legacy = obj(obj(context.credentials.get('scopes')).get('_taskflow_vivo_sign'))
            legacy_keys = ('sign_' + code, 'sign_wallet') if code == 'walletWelfare' else ('sign_' + code,)
            if any((obj(legacy.get(k)).get('state') == 'pending' for k in legacy_keys)):
                entries.append({'name': name, 'status': 'action_required', 'message': '此前业务入口保留未确认签到，仅核对该业务，不从新入口重发；其他业务继续。', 'reason_kind': 'settlement'})
                continue
            if journal.saved(rid).get('state') == 'pending':
                entries.append({'name': name, 'status': 'action_required', 'message': '此业务原签到尚未确认，仅回查该项，其他业务继续。', 'reason_kind': 'settlement'})
                continue
            receipt = journal.submit(rid, lambda: client._call(SIGNHOST, '/api/sign/dosign.do', method='POST', params={'sysCode': code}, pkg=package, writes=True), before={'today_signed': False, 'package': package})
            processed += 1
            if not yes(read()['todayIsSign']):
                raise UnknownWriteResult
            journal.confirm(rid)
            client.http.writing = False
            snapshot = {**snapshot, **adapter.profile(client).snapshot}
            entries.append({'name': name, 'status': 'success', 'message': '已按当期业务代码完成签到，今日状态已回读', 'biz_code': code, 'award_count': len(rows(obj(receipt.get('data')).get('awards')))})
        except CredentialsExpired:
            entries.append({'name': name, 'status': 'action_required', 'message': '本业务独立签到会话不可用，本项跳过，其他业务继续。', 'reason_kind': 'session_unavailable'})
        except ActionRequired as error:
            client.http.writing = False
            if error.diagnostics.get('provider_code') == '20041' and package == 'com.vivo.wallet':
                snapshot, replacement = wallet_replacement(adapter, client, context, snapshot, name)
                entries.append(replacement)
                continue
            entries.append({'name': name, 'status': 'action_required', 'message': error.message, **error.diagnostics})
        except UnknownWriteResult:
            client.http.writing = False
            entries.append({'name': name, 'status': 'action_required', 'message': '本项原签到已提交但结果未确认，保留原意图不重发；其他业务继续。', 'reason_kind': 'settlement'})
        except ProtocolError as error:
            client.http.writing = False
            pending = journal.saved(rid).get('state') == 'pending'
            entries.append({'name': name, 'status': 'action_required' if pending else 'failed', 'message': '本项提交后的回读暂时异常，保留原意图且不重发，其他业务继续。' if pending else error.message, 'reason_kind': 'settlement' if pending else 'failure', **({} if pending else error.diagnostics)})
    result = report(snapshot, entries, '已按官方当期业务清单逐项签到与核验')
    return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
