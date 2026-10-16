# Bundled vivo protocol reference; see source-manifest.json.
"""vivo+ and official App task center, with account-bound cookies and readback."""
import time
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse
from .compat import timezone
from .added_common import AddedAdapter, ClientLifecycle, Journal, checked, identity, obj, report, rows, title, yes
from .protocol_common import ActionRequired, GuardedSession, ProtocolError, amount, require_header
from .sdk import CredentialsExpired, StepResult, TaskSpec, UnknownWriteResult
from .vendor.vivo.taskcenter import BIZ_PACKAGE, DEFAULT_PACKAGE, DEFAULT_SITE, H5_REFERER, POINTH5, POINTSDK, POINTWAP, SIGNHOST, TASK_GROUPS, TaskCenterApi
from .vivo_activities import TASKS as ACTIVITY_TASKS
from .vivo_appointments import TASKS as APPOINTMENT_TASKS
from .vivo_browser_tasks import TASKS as BROWSER_REAL_TASKS
from .vivo_business_signs import TASKS as PUBLISHED_SIGN_TASKS
from .vivo_client_tasks import TASKS as CLIENT_TASKS
from .vivo_earn import TASKS as EARN_TASKS
from .vivo_native_updates import TASKS as NATIVE_UPDATE_TASKS
from .vivo_sdk_tasks import TASKS as SDK_TASKS
from .vivo_shop_actions import TASKS as SHOP_ACTION_TASKS
from .vivo_source_flows import TASKS as SOURCE_TASKS
from .vivo_updates import TASKS as UPDATE_TASKS
BIZ_NAMES = dict(zip(BIZ_PACKAGE, ('应用商店', '游戏中心', '官网 App', '浏览器', '视频', '钱包', '主题', '音乐', '小游戏', '快应用', '智慧生活', 'vivo+'), strict=True))
TASKS = (TaskSpec('profile', '同步 vivo 账号与积分'), TaskSpec('sign_in', 'vivo+ 主签到', True), *(TaskSpec('sign_' + key, 'vivo+第12业务签到' if key == 'vivop' else name + '业务签到', True) for key, name in BIZ_NAMES.items()), *(TaskSpec('tasks_' + key, name + '浏览与领奖', True) for key, name in TASK_GROUPS.items()), *(TaskSpec('ec_' + key, name + ' EC 浏览与领奖', True) for key, name in BIZ_NAMES.items() if key != 'vivop'), TaskSpec('purchase_points', '领取已到账购物积分', True), TaskSpec('game_instant', '秒开游戏已达标奖励', True), TaskSpec('game_download', '游戏下载已达标奖励', True), TaskSpec('game_reserve', '游戏预约已达标奖励', True), TaskSpec('member', '查询会员等级与积分账本'), TaskSpec('activities', '查询商城活动与待领奖励'), TaskSpec('home_welfare', '领取当前首页免费福利', True), TaskSpec('pending_points', '领取未到账积分', True), TaskSpec('mall_events', '领取商城活动已达标积分', True), TaskSpec('app_tasks', '官网 App 全业务任务广场', True), TaskSpec('member_benefits', '领取现有会员免费券与权益', True), TaskSpec('member_zones', '查询会员四个权益专区'), *ACTIVITY_TASKS, *EARN_TASKS, *UPDATE_TASKS, *SDK_TASKS, *SOURCE_TASKS, *NATIVE_UPDATE_TASKS, *APPOINTMENT_TASKS, *CLIENT_TASKS, *PUBLISHED_SIGN_TASKS, *BROWSER_REAL_TASKS, *SHOP_ACTION_TASKS)

class VivoClient(TaskCenterApi, ClientLifecycle):

    def __init__(self, credentials, ensure_allowed=lambda: None):
        from .vivo_device_context import normalized_device
        credentials['device'] = normalized_device(credentials['device'])
        self.credentials = credentials
        self.session = credentials['session']
        self.ua = require_header(credentials['user_agent'])
        self.cfg, self.site = ({}, dict(DEFAULT_SITE))
        self.pointh5, self.pointwap, self.pointsdk, self.sign_host = (POINTH5, POINTWAP, POINTSDK, SIGNHOST)
        self.http = GuardedSession({'pointh5.vivo.com.cn', 'pointwap.vivo.com.cn', 'pointsdk.vivo.com.cn', 'sign.vivo.com.cn', 'mpc.vivo.com.cn', 'wxmall.vivo.com.cn', 'wukongapi.vivo.com.cn', 'zhan.vivo.com.cn', 'topic.vivo.com.cn', 'member.vivo.com.cn', 'gameact.vivo.com.cn', 'www.vivo.com.cn', 'bbs.vivo.com.cn'}, ensure_allowed)
        self.pkg = DEFAULT_PACKAGE

    def _cookies(self, *, pkg=None, with_vivo_account=False, with_device=True, profile=None):
        values = {'vvc_openid': self.session['openid'], 'vvc_r': self.session['vivotoken'], 'vvc_imei': self.credentials['device']['imei']}
        if pkg:
            values['vvc_pn'] = pkg
        if with_vivo_account:
            values.update(vivo_account_cookie_iqoo_openid=self.session['openid'], vivo_account_cookie_iqoo_vivotoken=self.session['vivotoken'])
        if profile == 'wukong_appointment':
            values.update(vivo_account_cookie_iqoo_openid=self.session['openid'], vivo_account_cookie_iqoo_vivotoken=self.session['vivotoken'], vivo_account_cookie_iqoo_authtoken=self.session['vivotoken'], vivo_account_cookie_iqoo_checksum=self.session['vivotoken'], vivo_account_cookie_iqoo_regioncode='CN', vivo_account_cookie_wechat_from='108', app_package='minip.vivop')
        return '; '.join((f'{k}={require_header(v, k)}' for k, v in values.items()))

    @property
    def device(self):
        return SimpleNamespace(imei=self.credentials['device']['imei'])

    def _call(self, host, path, *, method='GET', params=None, pkg='_current_', with_vivo_account=False, referer=None, writes=None, encrypted=False, extra_headers=None, common=True, form=False, cookie_profile=None):
        package = self.pkg if pkg == '_current_' else pkg
        if cookie_profile is not None and (cookie_profile != 'wukong_appointment' or host != 'https://wukongapi.vivo.com.cn'):
            raise ProtocolError('活动 Cookie 仅用于对应官方域名。')
        params = dict(params or {})
        if common and self.session.get('unionid') and (host not in ('https://mpc.vivo.com.cn', 'https://wxmall.vivo.com.cn')):
            params.setdefault('unionId', self.session['unionid'])
        reads = {'/api/wx/user/userInfo', '/api/wx/user/getUnreceivedPurchasePoints', '/api/gameact/gameList.do', '/api/gameact/ruleDesc.do', '/api/gameact/getRewardStatus.do'}
        writing = method == 'POST' and path not in reads if writes is None else writes
        self.http.writing = self.http.writing or writing
        headers = {'User-Agent': self.ua, 'Referer': referer or H5_REFERER, 'Cookie': self._cookies(pkg=package, with_vivo_account=with_vivo_account, profile=cookie_profile)}
        for name, value in (extra_headers or {}).items():
            if name not in ('aladdin_sign', 'X-Captcha-Ticket'):
                raise ProtocolError('活动请求头未列入允许范围。')
            headers[name] = require_header(value, name)
        if host in ('https://mpc.vivo.com.cn', 'https://wxmall.vivo.com.cn'):
            headers.update({'vivo-token': self.session['vivotoken'], 'content-type': 'application/x-www-form-urlencoded;charset=UTF-8'})
            if host == 'https://mpc.vivo.com.cn':
                headers['wxAppId'] = 'wxeb4a6201a90783f4'
        kwargs = {'params': {**(params or {}), 't': int(time.time() * 1000)}} if method == 'GET' else {'data': params or {}} if form or host in ('https://mpc.vivo.com.cn', 'https://wxmall.vivo.com.cn') else {'json': params or {}, 'params': {'t': int(time.time() * 1000)}}
        if form:
            headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
            kwargs['params'] = {'t': int(time.time() * 1000)}
        cipher = None
        if encrypted:
            from .vendor.vivo.maodun import MaodunCipher
            if method != 'POST':
                raise ProtocolError('商城加密请求必须使用 POST。')
            cipher = MaodunCipher()
            kwargs = {'json': {'jvq_param': cipher.encrypt(params or {}), 'pts_sys_ver': '20260921_v6.1.6'}, 'params': {'pts_sys_t': int(time.time() * 1000)}}
        response = self.http.request(method, host + path, headers=headers, **kwargs)
        try:
            value = response.json()
            if cipher and isinstance(value, dict) and value.get('jvq_response'):
                import json
                value = json.loads(cipher.decrypt(value['jvq_response']))
        except ValueError as exc:
            raise UnknownWriteResult if self.http.writing else ProtocolError from exc
        key = 'retcode' if 'retcode' in obj(value) else 'code'
        good = ('0', '200') if host in ('https://mpc.vivo.com.cn', 'https://wxmall.vivo.com.cn', 'https://mshopact.vivo.com.cn') else ('0',)
        if path in ('/api/sign/dosign.do', '/api/sign/customize/sign.do'):
            good += ('10003',)
        if path == '/api/sign/start.do':
            good += ('10002',)
        if path == '/api/gameact/gameList.do' and str(obj(value).get(key)) == '12706':
            return {'retcode': 0, 'data': {'games': []}}
        if host == 'https://wxmall.vivo.com.cn' and str(obj(value).get(key)) == '31140003':
            raise ActionRequired('官方确认本账号商城独立会话失效，跳过该会话的验收；其他积分任务继续。', provider_code='31140003', reason_kind='session_unavailable')
        checked(value, key=key, good=good, expired=('1010', '20002', '200000'), operation='vivo 任务', writing=self.http.writing)
        if value.get('login') is False:
            raise CredentialsExpired
        return value

    def ec_list(self, biz):
        self.pkg = BIZ_PACKAGE[biz]
        value = self._call(POINTWAP, '/api/task/ec/taskList.do', params={'city': '', 'from': 'EC', 'type': 0, 'businessCode': '', 'tasks': ''})
        data = obj(value.get('data'))
        if not isinstance(data.get('tasks'), list):
            raise ProtocolError('EC 任务表结构不完整。')
        return rows(data['tasks'])

    def group_list(self, group):
        value = self.task_list_by_type(group)
        data = value.get('data')
        if isinstance(data, list):
            return rows(data)
        data = obj(data)
        if isinstance(data.get('tasks'), list):
            return self.flatten(value) if any(('list' in t for t in rows(data['tasks']))) else rows(data['tasks'])
        if isinstance(data.get('list'), list):
            return rows(data['list'])
        raise ProtocolError('vivo 未返回完整的分类任务表。')

    def member(self, path, *, body=None, form=False, writes=False, method='POST'):
        self.http.writing = self.http.writing or writes
        headers = {'User-Agent': self.ua, 'Referer': 'https://member.vivo.com.cn/wap/index', 'Origin': 'https://member.vivo.com.cn', 'Cookie': self._cookies(pkg='com.vivo.space', with_vivo_account=True), 'channel': '1', 'vivo_member_source_type': 'VIVO_PLUS_MINI_APP', 'mina_openid': self.session['openid'], 'mina_vivotoken': self.session['vivotoken']}
        if form:
            headers['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
        response = self.http.request(method, 'https://member.vivo.com.cn/api' + path, headers=headers, **{'params': {'t': int(time.time() * 1000), **(body or {})}} if method == 'GET' else {'data': body or {}} if form else {'json': body or {}})
        try:
            value = response.json()
        except ValueError as exc:
            if self.http.writing:
                raise UnknownWriteResult from exc
            raise ActionRequired('会员专区未向当前连接返回业务数据，请在官方会员中心确认入口。', reason_kind='unavailable') from exc
        checked(value, key='retcode', expired=('1010', '20002'), writing=self.http.writing, operation='vivo 会员权益')
        if value.get('login') is False:
            raise CredentialsExpired
        return value

    def visit(self, url):
        for _ in range(4):
            try:
                parsed = urlparse(str(url))
                allowed = parsed.scheme == 'https' and parsed.hostname in self.http.hosts and (not parsed.username) and (not parsed.password) and (parsed.port in (None, 443))
            except ValueError:
                allowed = False
            if not allowed or '/api/' in parsed.path.lower() or any((w in parsed.path.lower() for w in ('receive', 'exchange', 'delete', 'reward'))):
                raise ActionRequired('该任务需要在官方客户端打开指定入口。', reason_kind='manual')
            response = self.http.get(url, headers={'User-Agent': self.ua}, review_redirect=True)
            if not 300 <= response.status_code < 400:
                return
            url = urljoin(url, response.headers.get('Location', ''))
        raise ProtocolError('任务页面跳转次数超过已核对范围。')

class LiveVivo(AddedAdapter):
    version = 'vivo/11'
    platform = 'vivo'
    tasks = TASKS
    client_class = VivoClient

    def profile(self, client):
        if client.session.get('profile_kind') == 'app':
            return self.app_profile(client)
        try:
            value = client._call('https://mpc.vivo.com.cn', '/api/wx/user/userInfo', method='POST')
        except ActionRequired as exc:
            if exc.diagnostics.get('provider_code') != '31140003':
                raise
            found = self.app_profile(client)
            client.session['profile_kind'] = 'app'
            return found
        data = obj(value.get('data'))
        points = amount(data.get('points'))
        if not data.get('phone'):
            raise CredentialsExpired
        if (data.get('openid') or data.get('openId')) and str(data.get('openid') or data.get('openId')) != client.session['openid']:
            raise CredentialsExpired
        return identity(client.session['openid'], {'points': points, 'balance_label': 'vivo 积分'}, 'vivo')

    def app_profile(self, client):
        value = client._call(POINTH5, '/api/mall/v5/home/header.do', pkg='com.vivo.space', with_vivo_account=True)
        if value.get('login') is not True:
            raise CredentialsExpired
        return identity(client.session['openid'], {'points': amount(obj(value.get('data')).get('points')), 'balance_label': 'vivo 积分'}, 'vivo')

    def run(self, client, context, key, snapshot):
        if key in {task.key for task in SHOP_ACTION_TASKS}:
            from .vivo_shop_actions import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in BROWSER_REAL_TASKS}:
            from .vivo_browser_tasks import execute
            return execute(self, client, context, key, snapshot)
        if key == 'published_business_signs':
            from .vivo_business_signs import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in CLIENT_TASKS}:
            from .vivo_client_tasks import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in NATIVE_UPDATE_TASKS}:
            from .vivo_native_updates import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in APPOINTMENT_TASKS}:
            from .vivo_appointments import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in SOURCE_TASKS}:
            from .vivo_source_flows import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in SDK_TASKS}:
            from .vivo_sdk_tasks import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in UPDATE_TASKS}:
            from .vivo_updates import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in EARN_TASKS}:
            from .vivo_earn import execute
            return execute(self, client, context, key, snapshot)
        if key in {task.key for task in ACTIVITY_TASKS}:
            from .vivo_activities import execute
            return execute(self, client, context, key, snapshot)
        if key == 'profile':
            return StepResult('success', '账号身份与积分已回读', snapshot)
        if key.startswith('sign_'):
            return self.sign(client, context, key, snapshot)
        if key.startswith(('tasks_', 'ec_')):
            return self.task_group(client, context, key, snapshot)
        if key.startswith('game_'):
            return self.games(client, context, key, snapshot)
        if key in ('home_welfare', 'pending_points', 'mall_events', 'app_tasks', 'member_benefits', 'member_zones'):
            from .vivo_rewards import execute
            return execute(self, client, context, key, snapshot)
        if key == 'purchase_points':

            def pending():
                value = client._call('https://wxmall.vivo.com.cn', '/api/wx/user/getUnreceivedPurchasePoints', method='POST')['data']
                return amount(value.get('points') if isinstance(value, dict) else value)
            before = pending()
            journal = Journal(context, 'vivo_purchase')
            if not before:
                journal.confirm('purchase')
                return StepResult('skipped', '没有待领取购物积分', snapshot)
            journal.submit('purchase', lambda: client._call('https://wxmall.vivo.com.cn', '/api/wx/user/receivedPurchasePoints', method='POST'), before=before)
            after = pending()
            current = self.profile(client).snapshot
            if after >= before or current['points'] < snapshot['points'] + before - after:
                raise UnknownWriteResult
            journal.confirm('purchase')
            return StepResult('success', '购物积分领取及余额已核验', {**snapshot, **current})
        if key == 'member':
            client.member_level()
            client.point_detail_for_month()
            return StepResult('success', '会员等级与本月积分账本已查询', snapshot, diagnostics={'entries': [{'name': '积分账本', 'status': 'success', 'message': '平台已返回本月记录'}]})
        if key == 'activities':
            entries = []
            for name, call in (('活动任务', client.activity_task_list), ('待领积分', client.unrecv_point), ('商城签到', client.sign_home_point_list), ('商城站点', client.site_config)):
                call()
                entries.append({'name': name, 'status': 'success', 'message': '查询完成'})
            return report(snapshot, entries)
        raise ProtocolError('未实现的 vivo 任务。')

    def sign(self, client, context, key, snapshot):
        main = key == 'sign_in'
        biz = key.removeprefix('sign_')
        read = (lambda: obj(client._call(POINTH5, '/api/sign/home.do', pkg='com.vivo.wallet').get('data'))) if main else (lambda: obj(client._call(SIGNHOST, '/api/sign/info.do', params={'sysCode': 'vivop'}, pkg='com.vivo.space').get('data'))) if biz == 'vivop' else lambda: obj(client.sign_info(biz).get('data'))
        field = 'hasSigned' if main else 'todayIsSign'
        before = read()
        if field not in before:
            raise ProtocolError('签到状态不完整，未提交。')
        journal = Journal(context, 'vivo_sign')
        if yes(before[field]):
            journal.confirm(key)
            return StepResult('skipped', '平台确认今日已签到', snapshot)
        if main:
            if not before.get('taskId'):
                raise ProtocolError('主签到缺少本期任务标识。')

            def write():
                return client._call(POINTH5, '/api/sign/start.do', method='POST', pkg='com.vivo.wallet', params={'taskId': before['taskId']})
        else:

            def write():
                return client.sign_do(biz, pkg='com.vivo.space' if biz == 'vivop' else BIZ_PACKAGE[biz] or None)
        try:
            journal.submit(key, write)
        except ActionRequired as exc:
            if not main and exc.diagnostics.get('provider_code') in ('20041', '10002'):
                client.http.writing = False
                return StepResult('skipped', '旧签到活动已迭代或当前未开放；新站点由悟空业务签到任务核验', snapshot, diagnostics={'skip_kind': 'unavailable', 'provider_code': exc.diagnostics['provider_code']})
            raise
        if not yes(read().get(field)):
            raise UnknownWriteResult
        journal.confirm(key)
        client.http.writing = False
        return StepResult('success', '签到完成，今日签到状态已回读', {**snapshot, **self.profile(client).snapshot})

    def task_group(self, client, context, key, snapshot, *, load=None, refresh_profile=True):
        ec = key.startswith('ec_')
        group = key.split('_', 1)[1]
        read = load or ((lambda: client.ec_list(group)) if ec else lambda: client.group_list(group))
        tasks = read()
        journal, entries, completed = (Journal(context, 'vivo_tasks'), [], 0)
        need_more = False
        wallet = snapshot['points']
        for task in tasks:
            if completed >= 4:
                need_more = True
                break
            ident = str(task.get('id') or '')
            label = title(task.get('name'), 'vivo 任务')
            if not ident:
                raise ProtocolError('平台任务缺少标识。')
            record = {'name': label, 'status': 'skipped', 'message': '已完成'}
            done = yes(task.get('taskComplete')) or (task.get('completedTimes') is not None and task.get('totalTime') is not None and (amount(task['completedTimes']) >= amount(task['totalTime']) > 0))

            def current_task():
                return next((t for t in read() if str(t.get('id')) == ident), None)
            unclaimed = amount(task.get('unreceviedPoints', 0))
            if unclaimed > 0:
                reward_key = key + ':reward:' + ident
                receipt = journal.submit(reward_key, lambda: client.ec_receive_point(task, business_code=group) if ec else client.receive_point(task), before={'unclaimed': unclaimed, 'points': wallet})
                after = current_task()
                if after is None or amount(after.get('unreceviedPoints', 0)) >= unclaimed:
                    raise UnknownWriteResult
                reward = amount(obj(receipt.get('data')).get('rewardPoints'))
                current = self.profile(client).snapshot
                if obj(receipt.get('data')).get('rewardPoints') in (0, '0') and current['points'] == wallet and (amount(after.get('rewardTimes', 0)) > amount(task.get('rewardTimes', 0))):
                    journal.put(reward_key, {'state': 'confirmed', 'reward_points': 0, 'expected_points': unclaimed, 'date': timezone.localdate().isoformat()})
                    client.http.writing = False
                    snapshot = {**snapshot, **current}
                    record.update(status='action_required', message=f'平台已结算本档，但返回0积分（原待领{unclaimed}分）；未计作积分到账', reason_kind='zero_reward')
                    entries.append(record)
                    continue
                if reward <= 0 or current['points'] < wallet + reward:
                    raise UnknownWriteResult
                journal.confirm(reward_key)
                client.http.writing = False
                wallet = current['points']
                snapshot = {**snapshot, **current}
                completed += 1
                need_more = need_more or amount(after.get('unreceviedPoints', 0)) > 0
                record.update(status='success', message=f'已领取{reward}积分，待领数和账号余额均已回读')
                entries.append(record)
                continue
            if not done:
                if ec and group == 'browser':
                    from .vivo_browser_tasks import execute_one, kind_for
                    kind = kind_for(task)
                    if kind:
                        try:
                            native_result = execute_one(self, client, context, task, kind, snapshot, read, key)
                            snapshot = native_result.snapshot
                            entries.extend(native_result.diagnostics.get('entries') or [{'name': label, 'status': native_result.status, 'message': native_result.message, **native_result.diagnostics}])
                            need_more |= bool(native_result.continue_after)
                        except ActionRequired as error:
                            client.http.writing = False
                            entries.append({'name': label, 'status': 'action_required', 'message': error.message, **error.diagnostics})
                        continue
                if any((word in label for word in ('支付', '购买', '充值', '付费', '订单', '借款', '贷款', '分享', '评论', '群聊', '消息回复', '邀请'))):
                    record.update(status='action_required', message='本项需要真实交易或本人内容、关系操作，未使用网页回调替代；不消费积分或资金。', reason_kind='manual')
                    entries.append(record)
                    continue
                plain_ec_web = ec and yes(task.get('webTask')) and label.startswith(('访问', '浏览', '查看')) and (not any((word in label for word in ('支付', '购买', '充值', '付费', '订单', '借款', '贷款', '分享', '评论', '消息', '邀请', '安装'))))
                if not yes(task.get('webTask')) or (not task.get('jumpURL') and (not plain_ec_web)):
                    if task.get('taskCatg') == 'sign':
                        business = next((b for b, pkg in BIZ_PACKAGE.items() if pkg == client.pkg), None)
                        state = client.sign_biz_list().get('data')
                        if isinstance(state, list) and any((t.get('bizCode') == business and yes(t.get('isSign')) for t in rows(state))):
                            record.update(message='当前业务签到表确认已签；旧任务页计数未同步')
                            entries.append(record)
                            continue
                    music_favorite = client.pkg == BIZ_PACKAGE['iMusic'] and '收藏' in label and ('听书' in label)
                    if label in ('加入聊天频道', '佩戴频道身份', '安装并体验应用') or music_favorite:
                        from .vivo_native_actions import MusicActions, NativeActions
                        native = (MusicActions if music_favorite else NativeActions)(context)
                        native_key = key + ':native:' + ident
                        try:
                            saved = journal.saved(native_key)
                            if saved.get('state') in ('pending', 'confirmed'):
                                record.update(status='action_required', message='原生操作已提交，当前只回读官方进度，不重复加入、佩戴或安装。', reason_kind='settlement')
                            else:
                                native.setup()
                                evidence = journal.submit(native_key, lambda: native.operate(task, label), before={'completed': task.get('completedTimes'), 'unclaimed': unclaimed})
                                journal.put(native_key, {'state': 'confirmed', 'evidence': evidence, 'date': timezone.localdate().isoformat()})
                                after = current_task()
                                changed = after and (yes(after.get('taskComplete')) or amount(after.get('completedTimes', 0)) > amount(task.get('completedTimes', 0)) or amount(after.get('unreceviedPoints', 0)) > unclaimed)
                                record.update(status='success' if changed else 'action_required', message='真实原生操作已执行，官方任务进度已回读；接续当前可领奖励。' if changed else '真实原生操作已执行，官方进度尚未更新，未计任务完成或收益。', reason_kind='normal' if changed else 'settlement')
                                need_more = need_more or bool(changed)
                        except ActionRequired as error:
                            record.update(status='action_required', message=error.message, **error.diagnostics)
                        finally:
                            native.close()
                        entries.append(record)
                        continue
                    if '资讯' in label:
                        reason = '当前没有待领积分；需在vivo浏览器完成真实资讯阅读，系统再回查领取'
                    elif '预约' in label:
                        reason = '尚未完成首次真实游戏预约；当前设备未提供游戏中心执行入口'
                    elif any((word in label for word in ('分享', '评论', '群聊', '消息回复'))):
                        reason = '当前没有待领积分；发布或发送内容需本人完成'
                    elif any((word in label for word in ('观看', '阅读', '搜索', '推送'))):
                        reason = '当前没有待领积分；需在对应官方应用完成实际阅读、观看或操作'
                    else:
                        reason = '当前没有待领积分；需满足该任务的安装、订单、游戏或身份资格'
                    record.update(status='action_required', message=reason, reason_kind='manual')
                    entries.append(record)
                    continue
                if not task.get('keyId'):
                    raise ProtocolError('网页任务缺少服务器下发的 keyId。')
                if task.get('jumpURL'):
                    client.visit(task['jumpURL'])
                journal.submit(key + ':web:' + ident, lambda: client.complete_web_task(task['keyId']), before={'completed': amount(task.get('completedTimes', 0)), 'unclaimed': unclaimed, 'task_id': ident, 'key_id': str(task['keyId'])})
                task_after = current_task()
                if task_after is None or not (yes(task_after.get('taskComplete')) or amount(task_after.get('completedTimes', 0)) > amount(task.get('completedTimes', 0)) or amount(task_after.get('unreceviedPoints', 0)) > unclaimed):
                    raise UnknownWriteResult
                journal.confirm(key + ':web:' + ident)
                client.http.writing = False
                task = task_after
                need_more = need_more or (not yes(task.get('taskComplete')) and amount(task.get('completedTimes', 0)) < amount(task.get('totalTime', 1)))
                completed += 1
                record.update(status='success', message='页面已访问，任务进度已回读' if task.get('jumpURL') else '平台网页任务回调已提交，官方进度与待领积分已回读')
            else:
                journal.confirm(key + ':web:' + ident)
            unclaimed = amount(task.get('unreceviedPoints', 0))
            if unclaimed > 0:
                reward_key = key + ':reward:' + ident
                receipt = journal.submit(reward_key, lambda: client.ec_receive_point(task, business_code=group) if ec else client.receive_point(task))
                after = current_task()
                if after is None or amount(after.get('unreceviedPoints', 0)) >= unclaimed:
                    raise UnknownWriteResult
                reward = amount(obj(receipt.get('data')).get('rewardPoints'))
                current = self.profile(client).snapshot
                if reward <= 0 or current['points'] < wallet + reward:
                    raise UnknownWriteResult
                journal.confirm(reward_key)
                client.http.writing = False
                wallet = current['points']
                snapshot = {**snapshot, **current}
                record.update(status='success', message=f'任务奖励已回读，本次{reward}积分')
                completed += 1
            entries.append(record)
        result = report({**snapshot, **self.profile(client).snapshot} if refresh_profile else snapshot, entries, '本组任务已逐项核对，未达成的前置保留在明细', completed=completed)
        return StepResult(result.status, result.message, result.snapshot, diagnostics={**result.diagnostics, **({'skip_kind': 'already_complete'} if result.status == 'skipped' and entries else {})}, continue_after=1 if need_more else None)

    def games(self, client, context, key, snapshot):
        mode = {'game_instant': 0, 'game_download': 1, 'game_reserve': 2}[key]
        reward_key = client.PLAY_METHOD_REWARD_KEY[mode]
        value = client.game_list(mode)
        data = value.get('data')
        if not isinstance(obj(data).get('games'), list):
            raise ProtocolError('游戏任务清单结构不完整。')
        games = rows(data['games'])
        entries, journal = ([], Journal(context, 'vivo_games'))
        claimed, more = (0, False)
        for game in games:
            status = obj(game.get(reward_key)).get('status')
            label = title(game.get('name'), '游戏任务')
            if str(status) != '1':
                entries.append({'name': label, 'status': 'skipped' if str(status) == '2' else 'action_required', 'message': '已领奖' if str(status) == '2' else '需本人完成游戏前置'})
                continue
            if claimed >= 4:
                more = True
                break
            gid = str(game.get('id') or game.get('gameId') or '')
            if not gid:
                raise ProtocolError('游戏任务缺少标识。')
            objective = client.game_objective(game, mode)
            journal.submit(key + gid, lambda: client.game_reward(game, play_method=mode, objective=objective))
            result = client.game_list(mode).get('data')
            after = rows(obj(result).get('games'))
            matched = next((g for g in after if str(g.get('id') or g.get('gameId')) == gid), None)
            if str(obj(obj(matched).get(reward_key)).get('status')) != '2':
                raise UnknownWriteResult
            journal.confirm(key + gid)
            client.http.writing = False
            claimed += 1
            entries.append({'name': label, 'status': 'success', 'message': '奖励领取状态已回读'})
        result = report(snapshot, entries, '游戏奖励按服务器达标状态核验')
        return StepResult(result.status, result.message, snapshot, diagnostics=result.diagnostics, continue_after=1 if more else None)
