# Bundled vivo protocol reference; see source-manifest.json.
"""Free shop actions from the official catalog, with task/asset readback."""
import hashlib
import json
import re
from urllib.parse import urlparse
from .compat import timezone
from .added_common import Journal, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, TaskSpec, UnknownWriteResult
from .vivo_activities import walk
from .vivo_native_earn import SHOP, Native
TASKS = (TaskSpec('shop_product_browse', '实际浏览官网商城商品详情', True), TaskSpec('shop_cart_add', '完成官网商城免费加购物车任务', True))

def kind_for(task):
    name = str(task.get('name') or '')
    if any((word in name for word in ('支付', '付费', '购买', '充值', '订单', '借款', '贷款', '分享', '评论', '邀请'))):
        return None
    if '购物车' in name and '加入' in name:
        return 'shop_cart_add'
    if '浏览' in name and '商品详情' in name:
        return 'shop_product_browse'
    if '浏览' in name and '收藏' in name:
        return 'shop_favorites_browse'
    return None

def products(native):
    found = []
    for node in walk(native.eden('/v3/home/index')):
        for value in node.values():
            if not isinstance(value, str):
                continue
            parsed = urlparse(value)
            if parsed.scheme == 'https' and parsed.hostname == 'shop.vivo.com.cn':
                match = re.search('/wap/product/(\\d+)\\?skuId=(\\d+)', value)
                if match:
                    found.append((match[1], match[2]))
    if not found:
        raise ActionRequired('当前官方目录没有可核对的商品编号，未使用抓包中的历史商品。', reason_kind='unavailable')
    return list(dict.fromkeys(found))

def detail(native, spu, sku):
    product = obj(native.call(SHOP, '/wap/fbApi/v5/product/firstScreen/getInfo.json', params={'spuId': spu}))
    data = obj(native.call(SHOP, '/wap/fbApi/v5/product/firstScreen/getDetail.json', params={'spuId': spu, 'skuId': sku, 'buyType': 1}))
    base = obj(data.get('commoditySkuBaseInfo'))
    if str(base.get('skuId')) != str(sku) or not product.get('commoditySpu'):
        raise ProtocolError('商品详情身份未核对，未提交浏览事件或加购。')
    return {'product_detail_sha256': hashlib.sha256(json.dumps({'product': product, 'detail': data}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}

def browse_events(client):
    data = client._call('https://pointsdk.vivo.com.cn', '/sdk/config/action/list', method='POST', writes=False, common=False, pkg='com.vivo.space', params={'pkgName': 'com.vivo.space'}).get('data')
    return [str(action['actionId']) for action in rows(obj(data).get('actions')) if action.get('actionId') and any((s.get('eventId') == '081|001|55|077' and s.get('condition') == [] and (not s.get('logic')) for s in rows(action.get('scene'))))]

def perform(client, context, task, key, journal, rid, read_points=None):
    native = Native(client)
    native.shop_session()
    evidence = {}
    if key == 'shop_favorites_browse':
        link = str(task.get('jumpURL') or '')
        parsed = urlparse(link)
        if parsed.scheme != 'https' or parsed.hostname != 'shop.vivo.com.cn' or parsed.path not in ('/wap/my/favorite', '/wap/my/favorites'):
            raise ActionRequired('当前任务未发布可核验的收藏列表入口。', reason_kind='missing_context')
        response = client.http.request('GET', link, headers={'User-Agent': client.ua, 'Cookie': client._cookies(pkg='com.vivo.space', with_vivo_account=True)})
        if response.status_code != 200 or len(response.content) < 100:
            raise ProtocolError('商城收藏列表未完整读取。')
        return {'favorites_page_sha256': hashlib.sha256(response.content).hexdigest()}
    spu, sku = products(native)[0]
    evidence.update(detail(native, spu, sku))
    if key == 'shop_cart_add':
        token = native.call(SHOP, '/wap/fbApi/v1/my/token')
        if not isinstance(token, str) or not token:
            raise ProtocolError('商城未返回本次防重票据。')
        cart = native.call(SHOP, '/wap/fbApi/v2/shopcart/query/list', params={'token': token})
        if any((str(node.get('skuId')) == sku for node in walk(cart))):
            raise ActionRequired('选中商品已在本人购物车中，未重复增加数量。', reason_kind='already_done')
        receipt = journal.submit(rid, lambda: native.call(SHOP, '/wap/fbApi/v2/shopcart/addCommodity', method='POST', params={'token': token}, body={'num': 1, 'mainSku': {'skuId': sku}, 'serviceSkuList': [], 'bundleSkuList': [], 'additionSkuList': []}, writes=True))
        client.http.writing = False
        after = native.call(SHOP, '/wap/fbApi/v2/shopcart/query/list', params={'token': token})
        if not receipt or not any((str(node.get('skuId')) == sku for node in walk(after))):
            raise UnknownWriteResult
        return {**evidence, 'cart_sku_read_back': True, 'order_submitted': False}
    actions = browse_events(client)
    if not actions:
        raise ActionRequired('商品详情已读取，但当前没有对应的官方浏览事件配置。', reason_kind='unavailable')
    reward_total = 0
    for action in actions:
        stage = rid + ':' + action
        saved = journal.saved(stage)
        if saved.get('state') != 'confirmed':
            uploaded = journal.submit(stage, lambda action=action: client._call('https://pointsdk.vivo.com.cn', '/sdk/action/upload', method='POST', common=False, pkg='com.vivo.space', params={'openid': client.session['openid'], 'token': client.session['vivotoken'], 'pkgName': 'com.vivo.space', 'actionId': action, 'count': '1', 'notifyPattern': '0'}))
            data = obj(uploaded.get('data'))
            notice = obj(data.get('sdkTaskNotifyVo'))
            safe_notice = {k: notice[k] for k in ('actionId', 'taskId', 'points', 'syncReceive', 'taskType') if k in notice}
            if str(notice.get('taskId')) != str(data.get('taskId')):
                safe_notice = {}
            journal.put(stage, {'state': 'confirmed', 'date': timezone.localdate().isoformat(), 'notice': safe_notice})
        client.http.writing = False
        notice = journal.saved(stage).get('notice', {})
        reward_key = stage + ':reward'
        expected = amount(notice.get('points', 0))
        if not read_points or str(notice.get('actionId')) != action or expected <= 0 or (notice.get('syncReceive') != 0) or (journal.saved(reward_key).get('state') in ('confirmed', 'rejected')):
            continue
        before = read_points()
        try:
            journal.submit(reward_key, lambda action=action: client._call('https://pointsdk.vivo.com.cn', '/sdk/task/receive/point', method='POST', common=False, pkg='com.vivo.space', params={'openid': client.session['openid'], 'token': client.session['vivotoken'], 'pkgName': 'com.vivo.space', 'actionId': action, 'notifyPattern': '0'}), before={'points': before, 'expected': expected})
        except ActionRequired as error:
            if error.diagnostics.get('provider_code') not in ('1001', '10002'):
                raise
            journal.put(reward_key, {'state': 'rejected', 'date': timezone.localdate().isoformat()})
            client.http.writing = False
            continue
        client.http.writing = False
        after = read_points()
        if after < before + expected:
            raise UnknownWriteResult
        journal.put(reward_key, {'state': 'confirmed', 'date': timezone.localdate().isoformat(), 'reward_points': expected, 'before_points': before, 'after_points': after})
        reward_total += expected
    return {**evidence, 'actual_product_events': len(actions), 'sdk_reward_points': reward_total}

def execute_one(adapter, client, context, task, key, snapshot, load, reward_key):
    tid = str(task['id'])
    journal = Journal(context, 'vivo_shop_actions')
    rid = f'{timezone.localdate()}:{key}:{tid}'
    saved = journal.saved(rid)
    if saved.get('state') not in ('pending', 'confirmed'):
        evidence = perform(client, context, task, key, journal, rid, read_points=lambda: adapter.profile(client).snapshot['points'])
        journal.put(rid, {'state': 'confirmed', 'date': timezone.localdate().isoformat(), 'evidence': evidence})
    client.http.writing = False
    current = adapter.profile(client).snapshot
    if current['points'] < snapshot['points']:
        raise UnknownWriteResult
    snapshot = {**snapshot, **current}
    fresh = next((t for t in load() if str(t.get('id')) == tid), None)
    if fresh and (yes(fresh.get('taskComplete')) or amount(fresh.get('unreceviedPoints', 0)) > 0 or amount(fresh.get('completedTimes', 0)) > amount(task.get('completedTimes', 0))):
        return adapter.task_group(client, context, reward_key, snapshot, load=lambda: [t for t in load() if str(t.get('id')) == tid])
    return StepResult('action_required', '已执行真实商城动作，官方尚未认定任务达标；不计积分收入且不重复加购', snapshot, diagnostics={'reason_kind': 'settlement', 'entries': [{'name': title(task.get('name')), 'status': 'action_required', 'message': '实际动作已提交，待官方任务状态更新', 'reason_kind': 'settlement'}]})

def execute(adapter, client, context, key, snapshot):
    from .vivo_client_tasks import catalog
    entries = []
    for reward_key, load in (('client_tasks_vivospace', lambda: catalog(client, 'vivospace')), ('ec_vivospace', lambda: client.ec_list('vivospace'))):
        for task in load():
            if kind_for(task) != key:
                continue
            try:
                if yes(task.get('taskComplete')) or amount(task.get('unreceviedPoints', 0)) > 0 or amount(task.get('completedTimes', 0)) >= amount(task.get('totalTime', 1)) > 0:
                    result = adapter.task_group(client, context, reward_key, snapshot, load=lambda tid=str(task['id']), load=load: [t for t in load() if str(t.get('id')) == tid])
                else:
                    result = execute_one(adapter, client, context, task, key, snapshot, load, reward_key)
                snapshot = result.snapshot
                entries.extend(result.diagnostics.get('entries', []))
            except ActionRequired as error:
                entries.append({'name': title(task.get('name')), 'status': 'action_required', 'message': error.message, **error.diagnostics})
    if not entries:
        return StepResult('skipped', '当前官方目录未发布此项商城任务', snapshot, diagnostics={'skip_kind': 'unavailable'})
    return report(snapshot, entries, '商城免费动作与任务进度已逐项核验')
