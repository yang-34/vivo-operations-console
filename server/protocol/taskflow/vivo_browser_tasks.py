# Bundled vivo protocol reference; see source-manifest.json.
"""Owned Browser UI work. Native SDK emits its own events; no counter uploads."""
import hashlib
import re
import shlex
import time
from .compat import timezone
from .added_common import Journal, obj, report, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import StepResult, TaskSpec
from .vivo_native_actions import NativeActions
KINDS = {'search': '实际完成浏览器搜索', 'video': '实际播放浏览器视频', 'navigation': '实际浏览顶部导航站点', 'notification': '开启浏览器通知并核验', 'novel': '实际阅读浏览器免费小说'}
TASKS = tuple((TaskSpec('browser_' + key, label, True) for key, label in KINDS.items()))

def kind_for(task):
    name = str(task.get('name') or '')
    if any((w in name for w in ('借款', '贷款', '支付', '购买', '充值'))):
        return None
    if '阅读' in name and '新闻' in name:
        return 'news'
    if '搜索' in name:
        return 'search'
    if '视频' in name and any((w in name for w in ('看', '播放'))):
        return 'video'
    if '导航' in name and any((w in name for w in ('点击', '浏览'))):
        return 'navigation'
    if any((w in name for w in ('通知', '推送'))) and '开' in name:
        return 'notification'
    if '推送' in name and '看' in name:
        return 'push_read'
    if '小说' in name and any((w in name for w in ('阅读', '看'))):
        return 'novel'
    return None

def playback(raw, package):
    """Read only this package's MediaSession. Never infer playing from a timer."""
    for block in re.split('\\n(?=\\s*Session #|\\s*\\S+ \\(userId=)', raw):
        packages = re.findall('(?:package|packageName)\\s*=\\s*([A-Za-z0-9._]+)', block)
        if not packages or set(packages) != {package}:
            continue
        match = re.search('PlaybackState\\s*\\{[^}]*?state=(\\d+),\\s*position=(\\d+),[^}]*?speed=([\\d.]+)', block)
        if match and match[1] == '3' and (float(match[3]) > 0):
            return int(match[2])
    return None

class BrowserActions(NativeActions):
    package = 'com.vivo.browser'
    identity_script = 'vivo-browser-identity.js'
    app_name = '浏览器'

    def foreground(self):
        self.verify_owner()
        state = self.adb('shell', 'dumpsys', 'window', 'windows')
        lines = state.splitlines()
        focus = [line for line in lines if 'mCurrentFocus' in line]
        if not focus:
            focus = [line for line in lines if 'mFocusedApp' in line]
        if not any((self.package in line for line in focus)):
            raise ActionRequired('浏览器已离开前台，本段不计实际执行。', reason_kind='native_context')

    def tap_node(self, node):
        self.foreground()
        coords = list(map(int, re.findall('\\d+', node.get('bounds', ''))))
        if len(coords) != 4:
            raise ProtocolError('浏览器原生控件位置不完整。')
        self.adb('shell', 'input', 'tap', str((coords[0] + coords[2]) // 2), str((coords[1] + coords[3]) // 2))

    def search(self, index):
        term = ('vivo', 'weather', 'astronomy', 'science', 'travel')[index % 5]
        self.click(('搜索或输入网址', '搜索或输入网址/书名', '搜索', '搜索框'))
        nodes = [n for n in self.ui().iter('node') if n.get('package') == self.package and n.get('class') == 'android.widget.EditText' and (n.get('enabled') == 'true')]
        if len(nodes) != 1:
            raise ActionRequired('当前浏览器没有唯一搜索输入框，未上报搜索。', reason_kind='native_context')
        self.tap_node(nodes[0])
        self.adb('shell', 'input', 'keycombination', 'KEYCODE_CTRL_LEFT', 'KEYCODE_A')
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_DEL')
        self.adb('shell', 'input', 'text', term)
        inputs = [n for n in self.ui().iter('node') if n.get('package') == self.package and n.get('class') == 'android.widget.EditText']
        if len(inputs) != 1 or inputs[0].get('text') != term:
            raise ActionRequired('搜索输入未准确回读，未提交搜索或上报。', reason_kind='native_context')
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_ENTER')
        time.sleep(3)
        self.foreground()
        return {'physical_search_submitted': True, 'query_sha256': hashlib.sha256(term.encode()).hexdigest()}

    def video(self):
        self.click(('视频', '小视频', '短视频'))
        self.click(('播放', '点击播放', '继续播放'))
        self.foreground()
        first = playback(self.adb('shell', 'dumpsys', 'media_session'), self.package)
        if first is None:
            raise ActionRequired('未检测到本账号浏览器的实际播放状态，未虚报观看时长。', reason_kind='native_context')
        self.begin_action()
        start = time.monotonic()
        previous, observed = (first, 0.0)
        for _ in range(6):
            time.sleep(5)
            self.foreground()
            position = playback(self.adb('shell', 'dumpsys', 'media_session'), self.package)
            if position is None or not 0 < position - previous <= 10000:
                raise ActionRequired('视频未持续播放，本段未记录为观看完成。', reason_kind='native_context')
            observed += (position - previous) / 1000
            previous = position
        return {'physical_video_played': True, 'observed_seconds': round(min(observed, time.monotonic() - start), 1)}

    def notification(self):
        self.foreground()
        self.click(('我的',))
        self.click(('设置',))
        self.click(('消息通知', '通知管理', '推送通知'))
        switches = [n for n in self.ui().iter('node') if n.get('package') == self.package and n.get('checkable') == 'true']
        if len(switches) != 1:
            raise ActionRequired('浏览器通知设置没有唯一开关，未修改其他设置。', reason_kind='native_context')
        if switches[0].get('checked') != 'true':
            self.tap_node(switches[0])
        self.verify_owner()
        self.adb('shell', 'am', 'start', '-a', 'android.settings.APP_NOTIFICATION_SETTINGS', '--es', 'android.provider.extra.APP_PACKAGE', self.package)
        controls = [n for n in self.ui().iter('node') if n.get('package') == 'com.android.settings' and n.get('checkable') == 'true' and n.get('resource-id', '').endswith(('switch_widget', 'switch_bar'))]
        if len(controls) != 1:
            raise ActionRequired('Android通知页没有唯一总开关，未更改其他权限。', reason_kind='native_context')
        if controls[0].get('checked') != 'true':
            node = controls[0]
            bounds = list(map(int, re.findall('\\d+', node.get('bounds', ''))))
            if len(bounds) != 4:
                raise ProtocolError('通知开关位置不完整。')
            self.verify_owner()
            self.adb('shell', 'input', 'tap', str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))
        nodes = list(self.ui().iter('node'))
        if not any((n.get('package') == 'com.android.settings' and n.get('resource-id') == controls[0].get('resource-id') and (n.get('checked') == 'true') and (n.get('checkable') == 'true') for n in nodes)):
            raise ActionRequired('Android尚未确认通知权限开启，未计任务完成。', reason_kind='native_context')
        self.adb('shell', 'input', 'keyevent', 'KEYCODE_BACK')
        return {'notification_settings_read_back': True}

    def operate_task(self, task, kind, index):
        self.setup()
        self.foreground()
        if kind == 'push_read':
            try:
                notifications = self.script.exports_sync.notifications()
            except Exception as error:
                raise ActionRequired('当前原生服务不能读取本应用通知，本项跳过验收，未打开通知或上报。', reason_kind='session_unavailable') from error
            if not notifications:
                raise ActionRequired('本账号浏览器当前没有可打开的真实推送通知。', reason_kind='unavailable')
            self.adb('shell', 'cmd', 'statusbar', 'expand-notifications')
            root = self.ui()
            parents = {child: parent for parent in root.iter() for child in parent}
            for notice in notifications:
                matched = [n for n in root.iter('node') if n.get('package') == 'com.android.systemui' and n.get('text') == notice['title'] and (n.get('enabled') == 'true')]
                if len(matched) != 1:
                    continue
                node = matched[0]
                parent = node
                owned = False
                for _ in range(5):
                    parent = parents.get(parent)
                    if parent is None:
                        break
                    if any((n.get('text') == notice['app_label'] and n.get('resource-id', '').endswith(('app_name_text', 'header_text')) for n in parent.iter('node'))):
                        owned = True
                        break
                if not owned:
                    continue
                self.verify_owner()
                bounds = list(map(int, re.findall('\\d+', node.get('bounds', ''))))
                if len(bounds) != 4:
                    continue
                self.adb('shell', 'input', 'tap', str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))
                time.sleep(2)
                self.foreground()
                return {'physical_push_opened': True, 'notification_id': notice['id'], 'title_sha256': hashlib.sha256(notice['title'].encode()).hexdigest()}
            self.adb('shell', 'cmd', 'statusbar', 'collapse')
            raise ActionRequired('通知栏未找到唯一且归属于本浏览器的推送控件，未打开其他应用消息。', reason_kind='native_context')
        if kind == 'news':
            from urllib.parse import urlparse
            article = obj(task.get('article'))
            parsed = urlparse(str(article.get('url') or ''))
            if parsed.scheme != 'https' or parsed.hostname != 'vivo.yidianzixun.com' or parsed.query or (not re.fullmatch('/article/[A-Za-z0-9]+', parsed.path)) or (not article.get('title')):
                raise ProtocolError('当前原生资讯入口未通过核对。')
            self.adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d', shlex.quote(article['url']), '-p', self.package)
            time.sleep(3)
            self.foreground()
            text = ''.join((n.get('text', '') for n in self.ui().iter('node') if n.get('package') == self.package))
            if article['title'] not in text or len(text) < 100:
                raise ActionRequired('原生浏览器未确认文章正文已载入，未记作阅读完成。', reason_kind='native_context')
            time.sleep(5)
            self.adb('shell', 'input', 'swipe', '500', '1200', '500', '400', '500')
            return {'physical_article_read': True, 'article_id': article['docId'], 'body_sha256': hashlib.sha256(text.encode()).hexdigest()}
        if kind == 'search':
            return self.search(index)
        if kind == 'video':
            return self.video()
        if kind == 'notification':
            return self.notification()
        if kind == 'navigation':
            self.click(('首页',))
            nodes = [n for n in self.ui().iter('node') if n.get('package') == self.package and n.get('clickable') == 'true' and re.search('(?:top_nav|nav_site|navigation_item|site_item)', n.get('resource-id', ''), re.I) and n.get('text')]
            if not nodes:
                raise ActionRequired('当前顶部导航没有可识别的真实站点控件。', reason_kind='native_context')
            self.tap_node(nodes[index % len(nodes)])
            time.sleep(3)
            self.foreground()
            return {'physical_navigation_opened': True}
        if kind == 'novel':
            if not self.click(('小说', '免费小说')) or not self.click(('免费阅读', '立即免费阅读')):
                raise ActionRequired('当前没有唯一的免费小说阅读入口，未购买章节。', reason_kind='native_context')
            pages = []
            for _ in range(3):
                self.foreground()
                text = ''.join((n.get('text', '') for n in self.ui().iter('node') if n.get('package') == self.package))
                if len(text) < 100 or not re.search('第.{1,15}章', text) or any((w in text for w in ('购买章节', '付费阅读', '开通会员'))):
                    raise ActionRequired('未确认免费正文，未填报小说阅读完成。', reason_kind='native_context')
                pages.append(hashlib.sha256(text.encode()).hexdigest())
                time.sleep(5)
                self.adb('shell', 'input', 'swipe', '500', '1200', '500', '400', '500')
            if len(set(pages)) < 2:
                raise ActionRequired('正文未翻页，未记录小说阅读完成。', reason_kind='native_context')
            return {'real_free_pages': len(set(pages))}
        raise ProtocolError('未注册的浏览器原生任务。')

def execute_one(adapter, client, context, task, kind, snapshot, load, reward_key):
    tid = str(task['id'])
    journal = Journal(context, 'vivo_browser_real')
    prefix = f'{timezone.localdate()}:{tid}:'
    count = sum((k.startswith(prefix) and obj(v).get('state') == 'confirmed' for k, v in obj(context.credentials.get('scopes', {}).get(journal.namespace)).items()))
    key = prefix + str(count)
    if count >= 15:
        return StepResult('action_required', '今日真实动作已达到重试上限，等待官方任务进度同步；其他任务继续', snapshot, diagnostics={'reason_kind': 'settlement'})
    driver = BrowserActions(context)
    try:
        try:
            driver.setup()
        except ActionRequired as error:
            if error.diagnostics.get('reason_kind') != 'session_unavailable':
                raise
            return StepResult('skipped', error.message, snapshot, diagnostics={'skip_kind': 'native_unavailable', 'entries': [{'name': title(task.get('name')), 'status': 'skipped', 'message': error.message, 'skip_kind': 'native_unavailable'}]})
        if journal.saved(key).get('state') == 'pending':
            raise ActionRequired('上次原生动作结果不明，仅回查官方进度；其他任务继续。', reason_kind='settlement')
        if kind == 'news':
            from .vivo_news import News
            used = {obj(obj(v).get('evidence')).get('article_id') for k, v in obj(context.credentials.get('scopes', {}).get(journal.namespace)).items() if k.startswith(prefix)}
            article = next((a for a in News(client).catalogue() if a['docId'] not in used), None)
            if article is None:
                raise ActionRequired('当前没有新的真实资讯可读。', reason_kind='unavailable')
            task = {**task, 'article': article}
        driver.before_input = lambda: journal.put(key, {'state': 'pending', 'date': timezone.localdate().isoformat(), 'before': {'completed': task.get('completedTimes'), 'unclaimed': task.get('unreceviedPoints')}})
        evidence = driver.operate_task(task, kind, count)
        journal.put(key, {'state': 'confirmed', 'date': timezone.localdate().isoformat(), 'evidence': evidence})
    finally:
        driver.close()
    client.http.writing = False
    latest = next((t for t in load() if str(t.get('id')) == tid), None)
    changed = latest and (yes(latest.get('taskComplete')) or amount(latest.get('completedTimes', 0)) > amount(task.get('completedTimes', 0)) or amount(latest.get('unreceviedPoints', 0)) > amount(task.get('unreceviedPoints', 0)))
    if changed:
        claimable = yes(latest.get('taskComplete')) or amount(latest.get('unreceviedPoints', 0)) > 0 or amount(latest.get('completedTimes', 0)) >= amount(latest.get('totalTime', 1)) > 0
        if not claimable:
            return StepResult('success', '真实原生动作已执行，官方进度已增加，尚未达标；随后继续实际执行', {**snapshot, **adapter.profile(client).snapshot}, diagnostics={'native_evidence': evidence, 'entries': [{'name': title(task.get('name')), 'status': 'success', 'message': '真实动作与官方进度增加已核验，尚未计作任务完成'}]}, continue_after=1)
        result = adapter.task_group(client, context, reward_key, snapshot, load=lambda: [t for t in load() if str(t.get('id')) == tid])
        return StepResult(result.status, '真实原生动作已执行，官方任务进度和奖励已回读', result.snapshot, diagnostics={**result.diagnostics, 'native_evidence': evidence}, continue_after=result.continue_after)
    return StepResult('action_required', '真实原生动作已执行，官方尚未认定达标；未计收益', snapshot, diagnostics={'reason_kind': 'settlement', 'native_evidence': evidence}, continue_after=1 if count < 14 else None)

def execute(adapter, client, context, key, snapshot):
    from .vivo_client_tasks import catalog
    kind = key.removeprefix('browser_')
    entries, more = ([], False)
    for source, load in (('client_tasks_browser', lambda: catalog(client, 'browser')), ('ec_browser', lambda: client.ec_list('browser'))):
        for task in load():
            if kind_for(task) != kind:
                continue
            if yes(task.get('taskComplete')) or amount(task.get('completedTimes', 0)) >= amount(task.get('totalTime', 1)) > 0 or amount(task.get('unreceviedPoints', 0)) > 0:
                result = adapter.task_group(client, context, source, snapshot, load=lambda tid=str(task['id']), load=load: [t for t in load() if str(t.get('id')) == tid])
            else:
                try:
                    result = execute_one(adapter, client, context, task, kind, snapshot, load, source)
                except ActionRequired as error:
                    entries.append({'name': title(task.get('name')), 'status': 'action_required', 'message': error.message, **error.diagnostics})
                    continue
            snapshot = result.snapshot
            entries.extend(result.diagnostics.get('entries') or [{'name': title(task.get('name')), 'status': result.status, 'message': result.message, **result.diagnostics}])
            more |= bool(result.continue_after)
    if not entries:
        return StepResult('skipped', '当前官方目录未发布本项任务', snapshot, diagnostics={'skip_kind': 'unavailable'})
    result = report(snapshot, entries, KINDS[kind] + '已逐项执行并核验')
    return StepResult(result.status, result.message, result.snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
