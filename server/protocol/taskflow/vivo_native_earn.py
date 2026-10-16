# Bundled vivo protocol reference; see source-manifest.json.
"""Real catalogue favourites and real community reads with original device identity."""
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlparse
from .compat import timezone
from .added_common import Journal, checked, obj, report, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount, require_header
from .sdk import CredentialsExpired, StepResult, UnknownWriteResult
from .vendor.vivo import wave
from .vivo_activities import walk
EDEN = 'https://eden.vivo.com.cn'
SHOP = 'https://shop.vivo.com.cn'
MSHOP = 'https://mshopact.vivo.com.cn'
BBS = 'https://bbs.vivo.com.cn'

class Native:

    def __init__(self, client):
        self.client = client
        self.cookies = {}
        client.http.hosts |= {'eden.vivo.com.cn', 'shop.vivo.com.cn', 'mshopact.vivo.com.cn', 'bbs.vivo.com.cn'}

    def call(self, host, path, *, method='GET', params=None, form=None, body=None, writes=False):
        if host not in (EDEN, SHOP, MSHOP, BBS):
            raise ProtocolError('未支持的官网业务域。')
        headers = {'User-Agent': self.client.ua, 'Referer': SHOP + '/wap/index.html', 'Cookie': self.client._cookies(pkg='com.vivo.space', with_vivo_account=True)}
        if self.cookies:
            headers['Cookie'] += '; ' + '; '.join((f'{k}={require_header(v)}' for k, v in self.cookies.items()))
            if host == MSHOP and writes:
                headers['X-CSRF-TOKEN'] = require_header(self.cookies.get('vivo_fe_vftcookid'), '商城防重票据')
        kwargs = {'params': params or {}}
        if form is not None:
            kwargs['data'] = form
        if body is not None:
            kwargs['json'] = body
        self.client.http.writing = self.client.http.writing or writes
        response = self.client.http.request(method, host + path, headers=headers, **kwargs)
        try:
            value = response.json()
        except ValueError as exc:
            raise UnknownWriteResult if self.client.http.writing else ProtocolError from exc
        if host == EDEN and obj(value).get('result') is True:
            return value.get('value')
        key = 'retcode' if 'retcode' in obj(value) else 'code'
        checked(value, key=key, good=('200', '0') if host == MSHOP else ('0',), operation='vivo 官网任务', writing=self.client.http.writing)
        return value.get('data')

    def eden(self, path, fields=None):
        device = self.client.credentials['device']
        required = ('imei', 'sdk_int', 'android_ver', 'model', 'brand', 'density', 'screensize', 'net_type')
        if any((k not in device for k in required)):
            raise ActionRequired('官网业务需要该账号原始设备资料，请补充原会话设备信息。', reason_kind='missing_context')
        fields = fields or {}
        data = {'imei': device['imei'], 'oaid': device.get('oaid', ''), 'vaid': device.get('vaid', ''), 'aaid': device.get('aaid', ''), 'u': '' if int(device['sdk_int']) >= 29 else device.get('vaid', ''), 'model': device['model'], 'brand': device['brand'], 'av': str(device['sdk_int']), 'an': device['android_ver'], 'elapsedtime': str(int(time.monotonic() * 1000) % (1 << 31)), 'pkgName': 'com.vivo.space', 'appstore': '', 'pictype': 'webp', 'app_ver': '13415', 'density': device['density'], 'screensize': device['screensize'], 'nt': device['net_type'], 'signature': '', 'gitId': 'b80d0b7c310fcfe9e274db81863eba4b33a2b503', **fields}
        data = {k: str(v) for k, v in data.items()}
        data['sign'] = wave.m_value_for_post(data, wave.FORUM_SIG_KEY)
        return self.call(EDEN, path, method='POST', form=data)

    def shop_session(self):
        data = obj(self.call(MSHOP, '/tool/config'))
        try:
            cookies = obj(json.loads(data.get('tempCookies', '{}')))
        except (ValueError, TypeError) as exc:
            raise ProtocolError('商城会话引导格式未识别。') from exc
        for source, target in (('cookieId', 'vivo_fe_vftcookid'), ('fakeSessionId', 'vivo_fe_vftsessionid')):
            self.cookies[target] = require_header(cookies.get(source), '商城临时会话')
        who = obj(self.call(MSHOP, '/identity/query'))
        if str(who.get('openId')) != self.client.session['openid']:
            raise CredentialsExpired

    def favourite(self, spu, sku):
        token = self.call(SHOP, '/wap/fbApi/v1/my/token')
        if not isinstance(token, str) or not token:
            raise ProtocolError('商城没有返回本次防重票据。')
        return self.call(SHOP, '/wap/fbApi/v2/my/favorite/add', method='POST', params={'skuId': str(sku), 'spuIds': str(spu), 'token': token}, writes=True)

    def community(self, path, fields, *, writes=False):
        stamp = str(int(time.time() * 1000))
        nonce = hashlib.md5((self.client.device.imei + stamp + str(secrets.randbelow(1000))).encode()).hexdigest()
        body = {'sig': wave.get_sign(sorted([stamp, nonce, 'j56Hi8o1xzu6Gp'])), 'timestamp': stamp, 'nonce': nonce, **fields}
        return self.call(BBS, '/api/community/client/' + path, method='POST', body=body, writes=writes)

def execute(adapter, client, context, key, snapshot):
    client.pkg = 'com.vivo.space'
    group = '2' if key == 'shop_favorite' else '0'

    def tasks():
        return client.group_list(group)
    found = tasks()
    if key == 'shop_favorite':
        selection = [t for t in found if '收藏' in str(t.get('name', '')) and '商品' in str(t.get('name', ''))]
    else:
        selection = [t for t in found if '阅读' in str(t.get('name', '')) and any((w in str(t.get('name', '')) for w in ('帖子', '文章')))]
    if not selection:
        return StepResult('skipped', '当前官网任务表未下发此项积分任务', snapshot, diagnostics={'skip_kind': 'unavailable'})
    native, entries, journal = (Native(client), [], Journal(context, 'vivo_native_earn'))
    more = False
    for task in selection:
        tid = str(task.get('id') or '')
        if not tid:
            raise ProtocolError('官网任务缺少当前编号。')
        name = title(task.get('name'))
        action_key = tid if key == 'shop_favorite' else tid + ':' + timezone.localdate().isoformat()
        required_reads = 0
        if key == 'community_reads':
            target = re.search('(?:阅读|浏览)\\s*(\\d+)\\s*篇', name)
            if not target or not 1 <= int(target[1]) <= 20:
                raise ActionRequired('当前社区阅读任务没有明确的文章篇数。', reason_kind='missing_context')
            required_reads = int(target[1])

        def read_count():
            return sum((k.startswith(action_key + ':read:') and v.get('state') == 'confirmed' for k, v in obj(context.credentials.get('scopes', {}).get(journal.namespace)).items()))
        if yes(task.get('taskComplete')) or amount(task.get('unreceviedPoints', 0)) > 0:
            result = adapter.task_group(client, context, 'tasks_' + group, snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
            entries.extend(result.diagnostics.get('entries', []))
            snapshot = result.snapshot
            continue
        saved_state = journal.saved(action_key).get('state')
        if saved_state == 'pending' or (saved_state == 'confirmed' and (key == 'shop_favorite' or read_count() >= required_reads)):
            entries.append({'name': name, 'status': 'action_required', 'message': '此前已执行该动作，等待任务中心确认；未重复添加收藏或上报'})
            continue
        before = adapter.profile(client).snapshot['points']
        if key == 'shop_favorite':
            native.shop_session()
            home = native.eden('/v3/home/index')
            pairs = []
            for node in walk(home):
                for value in node.values():
                    if isinstance(value, str):
                        parsed = urlparse(value)
                        if parsed.hostname in ('shop.vivo.com.cn', 'mall.vivo.com.cn'):
                            hit = re.search('/wap/product/(\\d+)\\?skuId=(\\d+)', value)
                            if hit:
                                pairs.append((hit[1], hit[2]))
            if not pairs:
                raise ActionRequired('当前官方首页未返回可核对的商品编号对。', reason_kind='unavailable')
            receipt = journal.submit(action_key, lambda: native.favourite(*pairs[0]), before={'points': before})
            if not isinstance(receipt, dict):
                raise UnknownWriteResult
        else:
            data = native.eden('/client/bbk/activity/index', {'pageNum': '1', 'cfrom': '652'})
            articles = list(dict.fromkeys(re.findall('(?:/newbbs/thread/|/thread/)(\\d+)', json.dumps(data))))
            articles = [a for a in articles if journal.saved(action_key + ':read:' + a).get('state') != 'confirmed']
            remaining = required_reads - read_count()
            if not articles:
                raise ActionRequired('当前活动流未返回可阅读的真实文章。', reason_kind='unavailable')
            for article in articles[:min(remaining, 3)]:
                content = native.community('thread/detail', {'tid': article})
                if not isinstance(content, dict) or not content:
                    raise ProtocolError('文章正文未成功读取。')
                journal.submit(action_key + ':read:' + article, lambda: native.community('thread/incrReadNum', {'tid': article}, writes=True))
                journal.confirm(action_key + ':read:' + article)
                client.http.writing = False
            if read_count() < required_reads:
                more = True
                entries.append({'name': name, 'status': 'success', 'message': f'已实际读取{read_count()}/{required_reads}篇，继续剩余文章'})
                continue
            if yes(task.get('webTask')):
                observed = next((t for t in tasks() if str(t.get('id')) == tid), {})
                if not yes(observed.get('taskComplete')) and amount(observed.get('unreceviedPoints', 0)) == 0:
                    if not task.get('keyId'):
                        raise ProtocolError('当前网页阅读任务缺少服务器完成票据。')
                    journal.submit(action_key + ':finish', lambda: client.complete_web_task(task['keyId']))
        after_task = next((t for t in tasks() if str(t.get('id')) == tid), None)
        if after_task is None:
            raise UnknownWriteResult
        current = adapter.profile(client).snapshot
        if current['points'] < before:
            raise UnknownWriteResult
        client.http.writing = False
        snapshot = {**snapshot, **current}
        if yes(after_task.get('taskComplete')) or amount(after_task.get('unreceviedPoints', 0)) > 0:
            journal.confirm(action_key)
            if key == 'community_reads' and journal.saved(action_key + ':finish'):
                journal.confirm(action_key + ':finish')
            result = adapter.task_group(client, context, 'tasks_' + group, snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
            snapshot = result.snapshot
            entries.append({'name': name, 'status': 'success', 'message': f"实际动作与任务达标已回读，积分余额增加{snapshot['points'] - before}分"})
        elif key == 'community_reads' and amount(after_task.get('completedTimes', 0)) > amount(task.get('completedTimes', 0)):
            more = True
            entries.append({'name': name, 'status': 'success', 'message': '本批文章阅读进度已回读，继续剩余篇数'})
        else:
            journal.confirm(action_key)
            entries.append({'name': name, 'status': 'action_required', 'message': '实际收藏或文章读取已完成，任务中心尚未确认积分条件；未记作领奖成功'})
    result = report(snapshot, entries, '官网真实动作及对应积分已逐项核对')
    return StepResult(result.status, result.message, result.snapshot, diagnostics={**result.diagnostics, **({'skip_kind': 'already_complete'} if result.status == 'skipped' else {})}, continue_after=1 if more else None)
