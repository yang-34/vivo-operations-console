# Bundled vivo protocol reference; see source-manifest.json.
"""Official App mall free grants and its dynamically enumerated business tasks."""
from copy import deepcopy
from .compat import timezone
from .added_common import Journal, obj, report, rows, title
from .protocol_common import ActionRequired, ProtocolError, amount, decimal_amount
from .sdk import StepResult, UnknownWriteResult
from .vivo import BIZ_PACKAGE, POINTH5

def execute(adapter, client, context, key, snapshot):
    journal = Journal(context, 'vivo_mall_rewards')

    def call(path, **kwargs):
        return obj(client._call(POINTH5, path, **kwargs).get('data'))
    if key == 'member_zones':
        entries = []
        for zone_type, name in ((1, '售后权益'), (3, '积分权益'), (4, '生活权益'), (6, '成长权益')):
            client.member('/home/page/v5/homePageZoneInfo.do', body={'pageNo': 1, 'type': zone_type})
            entries.append({'name': name, 'status': 'success', 'message': '当前专区已查询'})
        return report(snapshot, entries, '会员权益专区已查询')
    if key == 'member_benefits':

        def read():
            data = obj(client.member('/home/page/v5/queryInfo.do', form=True).get('data'))
            zones = obj(data.get('zoneInfo'))
            if not isinstance(zones.get('zoneContentInfoList'), list):
                raise ProtocolError('会员中心未返回完整的权益列表。')
            found = [row for zone in rows(zones['zoneContentInfoList']) for row in rows(zone.get('zoneCommodityInfoList'))]
            for zone_type in (1, 3, 4, 6):
                for page_no in range(1, 21):
                    detail = obj(client.member('/home/page/v5/homePageZoneInfo.do', body={'pageNo': page_no, 'type': zone_type}).get('data'))
                    page_rows = [row for zone in rows(detail.get('zoneContentInfoList')) for row in rows(zone.get('zoneCommodityInfoList'))]
                    found.extend(page_rows)
                    if not page_rows or (not detail.get('hasNext') and page_no >= int(detail.get('totalPage', 1))):
                        break
                else:
                    raise ProtocolError('会员专区分页超出本轮范围，未将部分清单作为全量。')
            return list({(str(row.get('id')), str(row.get('receiveIds')), str(row.get('benefitId'))): row for row in found}.values())
        entries, completed, continuation = ([], 0, None)
        for benefit in read():
            if benefit.get('userStatus') != 0:
                continue
            if decimal_amount(benefit.get('pointsCost', 0)) or decimal_amount(benefit.get('price', 0)):
                continue
            if completed >= 3:
                continuation = 1
                break
            bid = benefit.get('id')
            if bid is None:
                raise ProtocolError('会员权益缺少标识。')

            def write():
                if benefit.get('receiveIds'):
                    ids = benefit['receiveIds']
                    if isinstance(ids, list):
                        ids = ','.join((str(i) for i in ids))
                    return client.member('/benefit/v5/receiveCoupon.do', body={'receiveIds': str(ids)}, form=True, writes=True)
                if benefit.get('benefitId'):
                    return client.member('/benefit/v5/receiveFlashBenefit.do', body={'benefitId': benefit['benefitId']}, form=True, writes=True)
                return client.member('/benefit/receiveByRisk.do', body={'benefitId': bid, 'version': '3.8'}, writes=True)
            rid = 'member:' + str(bid)
            if journal.saved(rid).get('state') == 'confirmed':
                continue
            saved = journal.saved(rid)
            if saved.get('state') == 'rejected' and saved.get('date') == timezone.localdate().isoformat():
                verification = saved.get('provider_code') == '21057'
                entries.append({'name': title(benefit.get('name')), 'status': 'action_required' if verification else saved.get('provider_result_status', 'failed'), 'message': '官方要求先完成滑块验证，未重复领取；其他权益继续' if verification else '本日该权益已被明确拒绝，其他权益继续核验', 'provider_code': saved.get('provider_code', ''), 'provider_message': saved.get('provider_message', '平台未返回明确原因'), 'reason_kind': 'verification' if verification else 'provider_rejected'})
                continue
            try:
                journal.submit(rid, write)
            except ActionRequired as exc:
                if exc.diagnostics.get('reason_kind') != 'provider_rejected':
                    raise
                verification = exc.diagnostics.get('provider_code') == '21057'
                journal.put(rid, {**journal.saved(rid), 'provider_code': exc.diagnostics.get('provider_code', ''), 'provider_message': exc.diagnostics.get('provider_message', '平台未返回明确原因'), 'provider_result_status': 'action_required' if verification else exc.diagnostics.get('provider_result_status', 'failed')})
                client.http.writing = False
                entries.append({'name': title(benefit.get('name')), 'status': 'action_required' if verification else exc.diagnostics.get('provider_result_status', 'failed'), 'message': '官方要求先完成滑块验证，本项未领取；其他权益继续' if verification else '本项权益领取被明确拒绝，其他权益继续核验', 'provider_code': exc.diagnostics.get('provider_code', ''), 'provider_message': exc.diagnostics.get('provider_message', '平台未返回明确原因'), 'reason_kind': 'verification' if verification else 'provider_rejected'})
                continue
            after = next((r for r in read() if r.get('id') == bid), None)
            if after is not None and after.get('userStatus') == 0:
                raise UnknownWriteResult
            journal.confirm(rid)
            client.http.writing = False
            completed += 1
            entries.append({'name': title(benefit.get('name')), 'status': 'success', 'message': '会员权益领取状态已回读'})
        result = report(snapshot, entries, '现有会员免费权益已核对')
        return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=continuation)
    if key == 'app_tasks':

        def listing(biz=''):
            data = call('/api/mall/task/center/tasklist.do', params={'type': 1, 'businessCode': biz})
            if not isinstance(data.get('tasks'), list):
                raise ProtocolError('官网任务广场未返回完整任务表。')
            return data
        root = listing()
        businesses = rows(root.get('businessInfos'))
        if not businesses:
            businesses = [{'businessCode': '', 'businessName': '官网任务广场'}]
        scopes = deepcopy(context.credentials.get('scopes', {}))
        cursor_key = '_taskflow_vivo_app_cursor'
        saved = obj(scopes.get(cursor_key))
        today = timezone.localdate().isoformat()
        index = int(saved.get('index', 0)) if saved.get('date') == today else 0
        if index >= len(businesses):
            index = 0
        entries, continuation = ([], None)
        for biz in businesses[index:index + 1]:
            code = str(biz.get('businessCode') or '')
            client.pkg = BIZ_PACKAGE.get(code) or 'com.vivo.space'
            result = adapter.task_group(client, context, 'app_' + code, snapshot, load=lambda: rows(listing(code)['tasks']))
            entries.extend(result.diagnostics.get('entries', []))
            snapshot = result.snapshot
            continuation = result.continue_after or continuation
        if not continuation:
            index += 1
        scopes = deepcopy(context.credentials.get('scopes', {}))
        scopes[cursor_key] = {'date': today, 'index': index if index < len(businesses) else 0}
        context.persist_scopes(scopes)
        continuation = continuation or (1 if index < len(businesses) else None)
        result = report(snapshot, entries, '官网任务广场各业务端已逐项核对')
        return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=continuation)
    if key == 'home_welfare':

        def read():
            return obj(call('/api/mall/v5/home/dynamic.do').get('welfare'))
        value = read()
        if not value.get('id'):
            return StepResult('skipped', '当前首页没有可领免费福利', snapshot)
        rid = str(value['id'])
        journal.submit(rid, lambda: call('/api/mall/v5/home/receiveWelfare.do', method='POST', params={'welfareId': value['id']}))
        after = read()
        current = adapter.profile(client).snapshot
        if after.get('id') == value['id'] or current['points'] <= snapshot['points']:
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        return StepResult('success', '首页福利消失及积分到账已回读', {**snapshot, **current})
    if key == 'pending_points':
        before = amount(call('/api/task/unrecvPoint.do').get('unrecvPoint'))
        if not before:
            journal.confirm(key)
            return StepResult('skipped', '当前没有待到账积分', snapshot)
        saved = journal.saved(key)
        if saved.get('state') == 'zero_grant' and saved.get('before') == before:
            return StepResult('action_required', '官方此前独立领取回执为0积分；本轮只回查待到账状态，不重复提交。', snapshot, diagnostics={'reason_kind': 'settlement'}, followup_after=900)
        receipt = journal.submit(key, lambda: call('/api/mall/tz/grant.do', method='POST'), before=before)
        after = amount(call('/api/task/unrecvPoint.do').get('unrecvPoint'))
        current = adapter.profile(client).snapshot
        if receipt.get('points') in (0, '0') and current['points'] == snapshot['points']:
            journal.put(key, {'state': 'zero_grant', 'date': timezone.localdate().isoformat(), 'before': before, 'receipt': {'points': 0}})
            client.http.writing = False
            return StepResult('action_required', '官方独立领取返回0积分，当前待到账可能为签到延迟；没有计收入，后台只回查结算状态。', {**snapshot, **current}, diagnostics={'reason_kind': 'settlement'}, followup_after=900)
        if after >= before or current['points'] < snapshot['points'] + before - after:
            raise UnknownWriteResult
        journal.confirm(key)
        client.http.writing = False
        return StepResult('success', '待到账积分已结算并回读', {**snapshot, **current})
    entries = []
    head = call('/api/mall/task/center/head.do')
    if not isinstance(head.get('mallEvens'), list):
        raise ProtocolError('商城活动列表结构不完整。')
    for event in rows(head['mallEvens']):
        event_key = event.get('eventKey')
        if not event_key:
            raise ProtocolError('商城活动缺少 eventKey。')
        state = call('/api/mall/event/eventdetail.do', params={'eventKey': event_key})
        unclaimed = state.get('unrecvPoint', state.get('unreceivedPoints'))
        if unclaimed is None:
            entries.append({'name': title(event.get('name')), 'status': 'action_required', 'message': '当前活动没有提供明确待领取积分'})
            continue
        if amount(unclaimed) == 0:
            journal.confirm(str(event_key))
            continue
        receipt = journal.submit(str(event_key), lambda: call('/api/mall/event/receive.do', method='POST', params={'eventKey': event_key}))
        after = call('/api/mall/event/eventdetail.do', params={'eventKey': event_key})
        remaining = after.get('unrecvPoint', after.get('unreceivedPoints'))
        if amount(receipt.get('receivePoint')) <= 0 or remaining is None or amount(remaining) >= amount(unclaimed):
            raise UnknownWriteResult
        journal.confirm(str(event_key))
        client.http.writing = False
        entries.append({'name': title(event.get('name')), 'status': 'success', 'message': '活动积分领取已回读'})
    return report(snapshot, entries, '商城活动待领奖励已逐项核对')
