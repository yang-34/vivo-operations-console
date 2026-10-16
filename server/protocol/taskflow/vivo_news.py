# Bundled vivo protocol reference; see source-manifest.json.
"""Read current Browser articles before reporting native detail-view events."""
import hashlib
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse
from .compat import timezone
from .added_common import Journal, checked, obj, report, rows, title
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, UnknownWriteResult
BROWSER = 'com.vivo.browser'
EVENT = '009|023|02|006'
SDK = 'https://pointsdk.vivo.com.cn'

class ArticleText(HTMLParser):

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.ignored = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if tag == 'article':
            self.depth += 1
        if tag in ('script', 'style'):
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag == 'article' and self.depth:
            self.depth -= 1
        if tag in ('script', 'style') and self.ignored:
            self.ignored -= 1

    def handle_data(self, data):
        if self.depth and (not self.ignored):
            self.text.append(data)

class News:

    def __init__(self, client):
        self.client = client
        client.http.hosts |= {'browser.vivo.com.cn', 'vivo.yidianzixun.com'}

    def catalogue(self):
        c, d = (self.client, self.client.credentials['device'])
        if any((k not in d for k in ('imei', 'oaid', 'vaid', 'model', 'net_type', 'screensize', 'android_ver', 'sdk_int', 'density'))):
            raise ActionRequired('资讯自动读取需要该账号原始设备资料，请补充完整会话包。', reason_kind='missing_context')
        params = {k: str(d[k]) for k in ('imei', 'oaid', 'vaid', 'model')}
        params.update({'clientVersion': '70271011', 'ver': '70271011', 'clientPackage': BROWSER, 'appId': '98', 'accountId': c.session['openid'], 'timestamp': str(int(time.time() * 1000)), 'showFeeds': '1', 'fromStore': '1', 'recommendType': '1', 'refreshType': '0', 'listRefreshCount': '0', 'loadTimes': '1', 'netType': d['net_type'], 'screensize': d['screensize'], 'an': d['android_ver'], 'av': str(d['sdk_int']), 'personalRecommend': '1', 'browserSystem': '1', 'browserSubSystem': '1', 'resolution': d['screensize'], 'pixel': d['density'], 'adrVer': str(d['sdk_int']), 'adrVerName': d['android_ver'], 'elapsedtime': str(int(time.monotonic() * 1000)), 'sysver': d['android_ver'], 'u': '', 'pver': '0', 'topicNewsCardNum': '3'})
        response = c.http.request('POST', 'https://browser.vivo.com.cn/mini/client/news/recommend.do', json=params, headers={'User-Agent': c.ua})
        try:
            value = response.json()
        except ValueError as exc:
            raise ProtocolError('资讯目录响应格式未识别。') from exc
        checked(value, key='retcode', operation='资讯目录')
        articles = obj(value.get('data')).get('normalNews')
        if not isinstance(articles, list):
            raise ProtocolError('资讯目录缺少文章列表。')
        return [a for a in rows(articles) if a.get('type') == 'doc' and a.get('docId') and (urlparse(str(a.get('url', ''))).hostname == 'vivo.yidianzixun.com')]

    def read(self, article):
        url = str(article.get('url', ''))
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'vivo.yidianzixun.com' or parsed.port not in (None, 443) or parsed.username or parsed.password or (not re.fullmatch('/article/[A-Za-z0-9]+', parsed.path)):
            raise ProtocolError('资讯正文链接不在已核验范围内。')
        response = self.client.http.request('GET', url, headers={'User-Agent': self.client.ua})
        parser = ArticleText()
        parser.feed(response.text)
        content = ''.join(parser.text).strip()
        if len(content) < 100 or str(article.get('title', '')) not in content:
            raise ProtocolError('资讯正文未完整读取，未上报阅读完成。')
        return {'body_sha256': hashlib.sha256(content.encode()).hexdigest(), 'characters': len(content)}

    def actions(self):
        value = self.client._call(SDK, '/sdk/config/action/list', method='POST', writes=False, common=False, pkg=BROWSER, params={'pkgName': BROWSER})
        actions = obj(value.get('data')).get('actions')
        if not isinstance(actions, list):
            raise ProtocolError('浏览器阅读事件配置未完整返回。')
        selected = [str(a['actionId']) for a in rows(actions) if a.get('actionId') and any((s.get('eventId') == EVENT and s.get('condition') == [] and (not s.get('logic')) for s in rows(a.get('scene'))))]
        if not selected:
            raise ActionRequired('当前未发布可匹配的资讯阅读积分事件。', reason_kind='unavailable')
        return list(dict.fromkeys(selected))

    def report(self, action):
        value = self.client._call(SDK, '/sdk/action/upload', method='POST', common=False, pkg=BROWSER, params={'openid': self.client.session['openid'], 'token': self.client.session['vivotoken'], 'pkgName': BROWSER, 'actionId': action, 'count': '1', 'notifyPattern': '1'})
        if obj(value.get('data')).get('disableUpload') is True:
            raise ActionRequired('平台暂停了此阅读事件的上报。', provider_code='UPLOAD_DISABLED', reason_kind='provider_rejected')
        return value

def execute(adapter, client, context, snapshot):

    def tasks():
        return [t for t in client.ec_list('browser') if '资讯' in str(t.get('name', ''))]
    api, journal, entries = (News(client), Journal(context, 'vivo_news'), [])
    more = False
    for task in tasks():
        tid = str(task.get('id') or '')
        goal = amount(task.get('processCnt', 0))
        if not tid or not 1 <= goal <= 20:
            raise ProtocolError('资讯任务缺少明确的篇数或任务编号。')
        before = adapter.profile(client).snapshot['points']
        if amount(task.get('unreceviedPoints', 0)) > 0:
            result = adapter.task_group(client, context, 'ec_browser', snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
            snapshot = result.snapshot
            entries.extend(result.diagnostics.get('entries', []))
            task = next((t for t in tasks() if str(t.get('id')) == tid), {})
        progress = amount(task.get('completedTimes', 0))
        if progress >= goal:
            settled = Journal(context, 'vivo_tasks').saved('ec_browser:reward:' + tid)
            zero = settled.get('reward_points') == 0 and settled.get('date') == timezone.localdate().isoformat()
            entries.append({'name': title(task.get('name')), 'status': 'action_required' if zero else 'skipped', 'message': f'当前阅读任务已达标{progress}/{goal}篇' + (f"；平台有一档结算0积分（原待领{settled['expected_points']}分）" if zero else '')})
            continue
        key = f'{tid}:{timezone.localdate()}:{progress}'
        saved = journal.saved(key)
        if saved.get('state') == 'confirmed':
            raise ActionRequired('阅读事件已提交，等待任务进度同步；未重复上报。', reason_kind='settlement')
        actions = api.actions()
        article = saved.get('article')
        if not article:
            used = {obj(v.get('article')).get('docId') for k, v in obj(context.credentials.get('scopes', {}).get(journal.namespace)).items() if k.startswith(f'{tid}:{timezone.localdate()}:')}
            article = next((a for a in api.catalogue() if a['docId'] not in used), None)
            if not article:
                raise ActionRequired('当前未找到未读的真实资讯。', reason_kind='unavailable')
            article = {k: article[k] for k in ('docId', 'url', 'title')}
            evidence = api.read(article)
            journal.put(key, {'state': 'read', 'article': article, **evidence})
        for action in actions:
            event_key = key + ':event:' + action
            if journal.saved(event_key).get('state') == 'confirmed':
                continue
            journal.submit(event_key, lambda: api.report(action), before={'docId': article['docId']})
            journal.confirm(event_key)
            client.http.writing = False
        journal.put(key, {**journal.saved(key), 'state': 'confirmed'})
        observed = next((t for t in tasks() if str(t.get('id')) == tid), {})
        if amount(observed.get('unreceviedPoints', 0)) > 0:
            result = adapter.task_group(client, context, 'ec_browser', snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
            snapshot = result.snapshot
            entries.extend((e for e in result.diagnostics.get('entries', []) if e.get('status') == 'action_required'))
            observed = next((t for t in tasks() if str(t.get('id')) == tid), {})
        current = adapter.profile(client).snapshot
        if current['points'] < before:
            raise UnknownWriteResult
        snapshot = {**snapshot, **current}
        after = amount(observed.get('completedTimes', 0))
        advanced = after > progress
        more = more or (advanced and after < goal)
        entries.append({'name': title(task.get('name')), 'status': 'success' if advanced else 'action_required', 'message': f"已读取真实资讯，任务进度{after}/{goal}篇，积分增加{current['points'] - before}分" if advanced else '正文读取与事件提交已完成，平台尚未确认阅读进度；未记作任务完成'})
    result = report(snapshot, entries, '资讯正文、分档进度与积分已逐项核对')
    return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
