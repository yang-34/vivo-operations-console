# Bundled vivo protocol reference; see source-manifest.json.
"""Reviewed source operations requiring a concrete asset/address/post selection."""
import hashlib
import json
import re
from urllib.parse import parse_qs, urlparse
from .added_common import Journal, checked, obj, rows, title, yes
from .confirmed_operations import collect_catalog, item
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, UnknownWriteResult
from .vivo_activities import ACTIVITY, Activities, walk
from .vivo_native_earn import BBS, Native
from .vivo_source_flows import call

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]

def bean_state(client):
    try:
        data = obj(call(client, '/api/bbsBean/index.do').get('data'))
    except ActionRequired as error:
        if error.diagnostics.get('provider_code') != '29999':
            raise
        raise ProtocolError('官方社区金豆查询返回29999（社区接口异常）；本人社区登录及原积分网关已核对，未把异常数据按0余额处理。', provider_code='29999', reason_kind='provider_unavailable') from error
    value = next((data[k] for k in ('bean', 'beans', 'beanNum') if k in data), None)
    if value is None:
        raise ProtocolError('社区金豆没有返回明确可用余额。')
    return {'beans': amount(value), 'data': data}

def community_posts(client):
    native = Native(client)
    posts = {}
    for page in (1, 2, 3):
        data = obj(native.eden('/client/bbk/activity/index', {'pageNum': str(page), 'cfrom': '652'}))
        found = rows(data.get('activities'))
        if not found:
            break
        for entry in found:
            links = str(entry.get('url') or '') + str(entry.get('cardActivity') or '')
            for a, b in re.findall('(?:/newbbs/thread/|/thread/)(\\d+)|[?&]tid=(\\d+)', links):
                posts.setdefault(a or b, title(entry.get('title') or entry.get('name'), '官方活动帖'))
    if not posts:
        response = client.http.get(BBS + '/recommend/homeRecommend', headers={'User-Agent': client.ua})
        for a, b in re.findall('(?:/thread/(\\d+)|tid=(\\d+))', response.text):
            posts.setdefault(a or b, '官方推荐帖')
    return list(posts.items())[:30]

def rank_keys(client):
    api = Activities(client)
    info = obj(api.quiz('user/info'))
    if not yes(info.get('openRewardFlag')):
        return []
    prizes = api.quiz('rank/prize/list')
    keys = set()
    for row in walk(prizes):
        if row.get('activityKey'):
            keys.add(str(row['activityKey']))
        for field in ('url', 'link', 'receiveUrl'):
            link = row.get(field)
            if not isinstance(link, str):
                continue
            parsed = urlparse(link)
            if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.vivo.com.cn'):
                continue
            keys.update(parse_qs(parsed.query).get('activityKey', []))
    return sorted((k for k in keys if re.fullmatch('[A-Za-z0-9_-]{1,100}', k)))

def rank_info(client, key):
    return obj(call(client, '/api/rank/prize/address.do', {'activityKey': key}, host=ACTIVITY).get('data'))

def receive_rank(client, body, validation):
    client.http.writing = True
    response = client.http.post(ACTIVITY + '/api/rank/prize/receive.do', params={'activityKey': body['activityKey'], 'addressId': body['addressId'], **{k: validation[k] for k in ('scene', 'ticket', 'constId') if validation.get(k)}}, headers={'User-Agent': client.ua, 'Referer': 'https://zhan.vivo.com.cn/point/wk240410b3206fb4', 'Cookie': client._cookies(pkg='com.vivo.space', with_vivo_account=True)})
    try:
        value = response.json()
    except ValueError as e:
        raise UnknownWriteResult from e
    checked(value, writing=True)
    return value

def catalog(provider, target=None, family=None):
    client = provider.client

    def beans():
        state = bean_state(client)
        balance = state['beans']
        return [item('vivo:beans-convert', '将当前社区金豆转换为积分', balance, '社区金豆', balance, {'kind': 'source_beans'}, eligible=balance > 0, reason='' if balance else '当前没有可转换社区金豆', rules='转换所显示的当前全部社区金豆，提交前重查数量；不扣vivo积分。此操作会消耗社区金豆，仅本人逐笔确认后执行。', terms={'quantity': 1, 'beans': balance, 'points_cost': 0})]

    def community():
        return [item('vivo:community-' + tid, name + ' · 帖子' + tid + ' · 点赞与收藏', 0, 'vivo 积分', provider.snapshot['points'], {'kind': 'source_community', 'tid': tid}, rules='所选官方帖子：https://bbs.vivo.com.cn/newbbs/thread/' + tid + ' 。只对该帖子点赞并收藏一次，真实打开帖文；会更改本人社区互动记录，不发表文字评论，不计作积分到账。', terms={'quantity': 1, 'post': tid, 'name': name, 'points_cost': 0}) for tid, name in community_posts(client)]

    def rank():
        offers = []
        for key in rank_keys(client):
            data = rank_info(client, key)
            address = obj(data.get('address'))
            prize = obj(data.get('prize'))
            if not prize:
                continue
            address_id = address.get('id')
            city = ''.join((str(address.get(k) or '') for k in ('province', 'city', 'area')))
            ready = bool(address_id)
            offers.append(item('vivo:rank-' + digest(key), title(prize.get('name') or prize.get('prizeName'), '榜单奖品') + ' · 已选地址' + ('（' + city + '）' if city else ''), 0, 'vivo 积分', provider.snapshot['points'], {'kind': 'source_rank', 'activityKey': key, 'addressId': address_id}, eligible=ready, reason='' if ready else '请先在官方奖品页选择本人的收货地址', rules='免费领取所显示的榜单奖品，使用官方返回的本人已选地址。提交前重查奖品和地址，领取成功须返回本人新订单编号，积分不减少。', terms={'quantity': 1, 'points_cost': 0, 'address': digest(address), 'prize': digest(prize)}))
        return offers
    families = {'beans': beans, 'community': community, 'rank': rank}
    if target:
        family = next((f for f in families if target.startswith('vivo:' + f + '-')), None)
        return [r for r in families[family]() if r['key'] == target] if family else []
    if family:
        return families[family]()
    return collect_catalog(provider, [(f, reader) for f, reader in families.items()])

def execute(provider, proposal):
    client, body = (provider.client, proposal['payload'])
    journal = Journal(provider.context, 'vivo_source_confirmed')
    before = provider.balance()
    if body['kind'] == 'source_beans':
        state = bean_state(client)
        beans = state['beans']
        if not beans or str(beans) != proposal['cost'] or beans != proposal['terms']['beans']:
            raise ActionRequired('当前金豆数量已变化，请重新确认。')
        rid = 'beans:' + str(beans) + ':' + digest(state['data'])
        journal.submit(rid, lambda: call(client, '/api/bbsBean/exchange.do', method='POST', writes=True), before={'beans': beans, 'points': before})
        after = bean_state(client)
        points = provider.balance()
        if after['beans'] != 0 or points <= before:
            raise UnknownWriteResult
        journal.confirm(rid)
        client.http.writing = False
        return StepResult('success', f'本人{beans}社区金豆已转换，积分{before}→{points}已回读', {**provider.snapshot, 'points': points}, client.updates())
    if proposal['cost'] != '0' or proposal['terms'].get('points_cost') != 0:
        raise ActionRequired('本项未核验为免费操作。')
    if body['kind'] == 'source_rank':
        data = rank_info(client, body['activityKey'])
        address = obj(data.get('address'))
        prize = obj(data.get('prize'))
        if str(address.get('id')) != str(body['addressId']) or digest(address) != proposal['terms']['address'] or digest(prize) != proposal['terms']['prize']:
            raise ActionRequired('本人奖品或收货地址已变化，未提交领取。')
        rid = 'rank:' + body['activityKey']
        response = journal.submit(rid, lambda: receive_rank(client, body, getattr(provider, 'confirmation_input', {})), before={'points': before})
        orders = response.get('data')
        points = provider.balance()
        if not isinstance(orders, list) or not any((obj(o).get('orderId') for o in orders)) or points < before:
            raise UnknownWriteResult
        journal.put(rid, {'state': 'confirmed', 'order_ids': [str(o['orderId']) for o in orders if obj(o).get('orderId')]})
        client.http.writing = False
        return StepResult('success', '所选榜单奖品已返回本人领取订单，地址匹配且积分未减少', {**provider.snapshot, 'points': points}, client.updates())
    if body['kind'] == 'source_community':
        tid = body['tid']
        if not any((post == tid for post, _ in community_posts(client))):
            raise ActionRequired('所选官方活动帖已不在当前目录，未发布互动。')
        native = Native(client)
        native.community('auth/auth', {'openid': client.session['openid'], 'vivotoken': client.session['vivotoken']}, writes=True)
        client.http.writing = False
        detail = native.community('thread/detail', {'tid': tid})
        if not isinstance(detail, dict):
            raise ProtocolError('所选帖子未返回可读取的详情。')
        rid = 'community:' + tid
        for name, path in (('like', 'thread/like'), ('favorite', 'thread/favorite')):
            stage = rid + ':' + name
            if journal.saved(stage).get('state') == 'confirmed':
                continue
            try:
                journal.submit(stage, lambda: native.community(path, {'tid': tid, 'type': 1}, writes=True))
            except ActionRequired as e:
                if e.diagnostics.get('provider_code') not in ('16002', '16016'):
                    raise
            journal.confirm(stage)
            client.http.writing = False
        native.community('thread/detail', {'tid': tid})
        points = provider.balance()
        if points < before:
            raise UnknownWriteResult
        journal.confirm(rid)
        return StepResult('success', '所选官方活动帖的点赞与收藏已获官方确认；未发布文字评论，未计作积分奖励', {**provider.snapshot, 'points': points}, client.updates())
    raise ProtocolError('没有对应的新增确认操作。')
