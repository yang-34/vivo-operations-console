# Bundled vivo protocol reference; see source-manifest.json.
"""Current native Game Center welfare and Wallet favour sign contracts."""
import json
import time
from urllib.parse import quote, unquote
from .compat import timezone
from .added_common import Journal, checked, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, require_header
from .sdk import CredentialsExpired, StepResult, TaskSpec, UnknownWriteResult
from .vendor.vivo import jvq, wave
MAIN = 'https://main.gamecenter.vivo.com.cn'
WRITE = 'https://w.gamecenter.vivo.com.cn'
WALLET = 'https://vivopay.vivo.com.cn'
FAVOUR = 'https://fop-active-center-prd.vivojrkj.com'
FLAGS = '111111111111111111110111'
TASKS = (TaskSpec('gc_native_activity_catalog', '发现游戏中心原生活动与福利'), TaskSpec('gc_native_week_sign', '游戏中心福利周签到', True), TaskSpec('gc_native_vip_tickets', '领取游戏中心已有VIP免费券', True), TaskSpec('gc_native_download_rewards', '领取游戏中心已达标下载福利', True), TaskSpec('wallet_native_assets', '核对钱包独立账号与积分'), TaskSpec('wallet_native_sign', '钱包省钱专区原生每日签到', True), TaskSpec('main_sign_reminder', '开启官网主签到提醒并核验', True))

def unavailable_tasks(credentials):
    """Credential availability only; never infer official reward eligibility."""
    session = obj(credentials.get('session'))
    wallet = obj(obj(credentials.get('scopes')).get('vivo_wallet'))
    result = set()
    if not (session.get('authtoken') and session.get('uuid')):
        result.update((t.key for t in TASKS if t.key.startswith('gc_native_')))
    if not (session.get('openid') and wallet.get('openid') == session['openid'] and all((wallet.get(k) for k in ('userToken', 'memberNo', 'token')))):
        result.update((t.key for t in TASKS if t.key.startswith('wallet_native_')))
    return result

class Game:

    def __init__(self, client):
        self.c = client
        if not client.session.get('authtoken') or not client.session.get('uuid'):
            raise ActionRequired('本账号游戏中心原生会话不可用，跳过该会话的福利验收；其他免费任务继续。', reason_kind='session_unavailable')
        from .vivo_game_reservation import Reservations
        identity = Reservations(client)
        try:
            identity.identity()
        except CredentialsExpired as error:
            raise ActionRequired('本账号游戏中心原生身份会话不可用，本次跳过该原生会话验收。', reason_kind='session_unavailable') from error
        self.uid = str(identity.account['id'])
        client.http.hosts |= {'main.gamecenter.vivo.com.cn', 'w.gamecenter.vivo.com.cn'}
        self.identity = identity
        who = obj(self.call('/clientRequest/user/submitUserLogin'))
        if str(who.get('userId') or '') not in (self.uid, client.session['openid']):
            raise ProtocolError('游戏中心原生业务未回读匹配的本人身份，未开放福利写入。')

    def call(self, path, body=None, *, write=False):
        if any((k in (body or {}) for k in ('userId', 'userid', 'openid', 'openId', 'vivotoken', 'token', 'systoken', 'validToken'))):
            raise ProtocolError('活动参数不能替换已核对的原生账号身份。')
        if path.startswith('/clientRequest/downloadWelfare/'):
            fields = dict(body or {})
            if write:
                fields['openId'] = self.c.session['openid']
            return obj(self.identity.call(path, fields, write=write, post=True)).get('data')
        c, d, s = (self.c, self.c.credentials['device'], self.c.session)
        host = WRITE if write else MAIN
        ua = f"Dalvik/2.1.0 (Linux; U; Android {d['android_ver']}; {d['model']})/vivo_game"
        params = {'u': d.get('ufsid', ''), 'oaid': d.get('oaid', ''), 'vaid': d.get('vaid', ''), 'aaid': d.get('aaid', ''), 'imei': d['imei'], 'model': d['model'], 'androidVer': str(d['sdk_int']), 'adrVerName': d['android_ver'], 'apppkgName': 'com.vivo.game', 'appVersion': str(d.get('game_app_version', '72802')), 'appVersionName': d.get('game_app_version_name', '7.2.80.2'), 'platformVersion': '109', 'platformVersionName': '1.09', 'elapsedtime': str(int(time.monotonic() * 1000)), 'pixel': d['density'], 'cs': '0', 'nt': d['net_type'], 'arcore': '1', 'sysAccount': '1', 'supportGifIcon': '1', 'vivoModel': '1', 'scoreFlag': '1', 'recommendModel': '1', 'agreePrivacy': '1', 'minorMode': '0', 'ageRange': '0', 'guestStatus': '0', 'patch_sup': '0', 'deviceType': d.get('device_type', '0'), 'picType': 'webp', 'userAgent': ua, 'openid': s['openid'], 'userid': self.uid, 'userId': s['openid'], 'vivotoken': s['vivotoken'], 'systoken': s['vivotoken'], 'validToken': s['vivotoken'], **(body or {})}
        params.update(token=s['authtoken'], uuid=s['uuid'])
        params['s'] = unquote(wave.m_value_for_post(params, host + path))
        c.http.writing = c.http.writing or write
        value = c.http.post(host + path, data=params, headers={'User-Agent': ua, 'Referer': 'http://www.vivo.com.cn', 'Cookie': 'userid=' + require_header(self.uid) + ';vivotoken=' + require_header(s['vivotoken']) + ';token=' + require_header(s['authtoken'])}).json()
        code = str(obj(value).get('retcode', obj(value).get('code', '')))
        if code in ('20001', '20002', '30100'):
            raise ActionRequired('游戏中心原生会话已不可用，本次跳过原生验收；其他免费任务继续。', reason_kind='session_unavailable', provider_code=code)
        if isinstance(value, dict) and value.get('result') is True:
            return value.get('data', value.get('value', value.get('msg')))
        checked(value, key='retcode' if 'retcode' in obj(value) else 'code', good=('0', '200'), writing=write)
        return value.get('data')

class Wallet:

    def __init__(self, client):
        self.c = client
        self.s = obj(obj(client.credentials.get('scopes')).get('vivo_wallet'))
        if any((not self.s.get(k) for k in ('openid', 'userToken', 'memberNo', 'token'))):
            raise ActionRequired('本账号钱包原生会话不可用，跳过该会话验收；其他免费积分任务继续。', reason_kind='session_unavailable')
        if self.s['openid'] != client.session['openid']:
            raise ActionRequired('钱包原生会话与当前vivo账号不一致，未提交操作。', reason_kind='session_unavailable')
        client.http.hosts |= {'vivopay.vivo.com.cn', 'fop-active-center-prd.vivojrkj.com'}
        me = obj(self.call(WALLET, '/user_center/user/info/query/memberbase'))
        if str(me.get('openid') or me.get('openId') or '') != client.session['openid']:
            raise ProtocolError('钱包接口未回读匹配的本人身份，尚未开放业务写入。', reason_kind='failure')

    def call(self, host, path, extra=None, *, write=False):
        if any((k in (extra or {}) for k in ('openid', 'userToken', 'memberNo', 'token'))):
            raise ProtocolError('钱包业务参数不能替换本账号会话。')
        d, s = (self.c.credentials['device'], self.s)
        body = {'model': d['model'], 'imei': d['imei'], 'androidVerCode': str(d['sdk_int']), 'androidVerName': d['android_ver'], 'ec': d.get('ec', ''), 'countryCode': 'CN', 'locale': 'zh_CN', 'cplc': '', 'nfcStatus': d.get('nfcStatus', '0'), 'pkgName': 'com.vivo.wallet', 'appVerName': '5.4.4.0', 'appVerCode': '54440', 'version': '1.0', 'flowSeqNo': str(int(time.time() * 1000)), 'deviceId': d.get('wallet_device_id', d['imei']), 'launch_flowid': '', 'oaid': d.get('oaid', ''), 'vaid': d.get('vaid', ''), **{k: require_header(str(s[k])) for k in ('openid', 'userToken', 'memberNo', 'token')}, **(extra or {})}
        joined = '&'.join((quote(k, safe='') + '=' + quote(str(v), safe='') for k, v in body.items()))
        packet = jvq.seckey_bytes(joined.encode(), key_version=6, pkg_name='com.vivo.wallet')
        self.c.http.writing = self.c.http.writing or write
        response = self.c.http.post(host + path, data={'jvq_param': jvq.b64_flag11(packet), 'jvqPlatform': 'h5'}, headers={'Content-Type': 'application/x-www-form-urlencoded', 'Origin': 'https://m.vivojrkj.com'})
        try:
            value = response.json()
        except ValueError:
            value = json.loads(jvq.decrypt_response(response.text))
        checked(value, good=('0', '200'), writing=write)
        return value.get('data')

def execute(adapter, client, context, key, snapshot):
    day = timezone.localdate().isoformat()
    if key == 'main_sign_reminder':
        return sign_reminder(client, context, snapshot)
    if key.startswith('wallet_'):
        wallet = Wallet(client)
        if key == 'wallet_native_assets':
            value = obj(wallet.call(WALLET, '/api/account/pointmsg/v3'))
            return StepResult('success', '钱包本人身份与独立积分入口已回读', snapshot, diagnostics={'entries': [{'name': '钱包积分', 'status': 'success', 'message': '独立资产已查询'}]})
        j = Journal(context, 'vivo_wallet_sign')
        state = obj(wallet.call(FAVOUR, '/welfare/home/sign_in/info/v2'))
        if type(state.get('todayIsSign')) is not bool:
            raise ProtocolError('钱包省钱签到未提供明确的今日状态。')
        if state['todayIsSign']:
            j.confirm(day)
            return StepResult('skipped', '官方确认今日钱包省钱签到已完成', snapshot)
        j.submit(day, lambda: wallet.call(FAVOUR, '/welfare/home/sign_in', write=True), before=state)
        if not yes(obj(wallet.call(FAVOUR, '/welfare/home/sign_in/info/v2')).get('todayIsSign')):
            raise UnknownWriteResult
        j.confirm(day)
        client.http.writing = False
        return StepResult('success', '钱包省钱签到已提交，今日状态已由官方回读', {**snapshot, **adapter.profile(client).snapshot})
    game = Game(client)
    j = Journal(context, 'vivo_native_game_welfare')
    if key == 'gc_native_activity_catalog':
        entries = []
        for label, path in (('游戏中心活动', '/clientRequest/activity/list'), ('物料活动', '/clientRequest/module/material/activity/list'), ('福利与积分签到', '/clientRequest/queryPointSign')):
            value = game.call(path, {'functionFlags': '10'} if 'queryPointSign' in path else {})
            entries.append({'name': label, 'status': 'success', 'message': f'官方当前入口已读取，目录条数{(len(value) if isinstance(value, list) else len(obj(value)))}'})
        return report(snapshot, entries, '游戏中心原生活动与福利已发现；查询不计为任务收益')
    if key == 'gc_native_week_sign':
        rid = day + ':weekly'
        if j.saved(rid).get('state') == 'confirmed':
            return StepResult('skipped', '今日游戏中心福利周签到已核验，不重复提交', snapshot)
        receipt = obj(j.submit(rid, lambda: game.call('/clientRequest/welfareTab/vipWeekTicket/checkin', {'functionFlags': FLAGS}, write=True)))
        check = obj(receipt.get('checkInData'))
        if 'days' not in check:
            raise UnknownWriteResult
        j.put(rid, {'state': 'confirmed', 'date': day, 'checkInData': check})
        client.http.writing = False
        return StepResult('success', '福利周签到已提交，官方签到回执已回读；未将空奖励计为新增积分', {**snapshot, **adapter.profile(client).snapshot})
    if key == 'gc_native_vip_tickets':
        entries = []
        for kind in ('1', '2', '3'):
            rid = day + ':vip:' + kind
            saved = j.saved(rid)
            if saved.get('state') == 'confirmed':
                continue
            if saved.get('state') == 'rejected':
                entries.append(saved.get('outcome', {'name': 'VIP福利券' + kind, 'status': 'failed', 'message': '原领取请求被明确拒绝，保留异常，不计作资格不足'}))
                continue
            try:
                receipt = j.submit(rid, lambda kind=kind: game.call('/clientRequest/welfareTab/vipTicket/batchReceive', {'ticketVipTypes': kind}, write=True))
                issued = rows(obj(receipt).get('ticketList'))
                j.put(rid, {'state': 'confirmed', 'date': day, 'receipt': receipt})
                entries.append({'name': 'VIP福利券' + kind, 'status': 'success' if issued else 'skipped', 'message': '官方返回本人福利券领取回执' if issued else '官方受理且未下发新券，没有记为奖励到账'})
            except ActionRequired as error:
                entry = {'name': 'VIP福利券' + kind, 'status': 'failed' if error.diagnostics.get('provider_result_status') == 'failed' else 'action_required', 'message': error.message, **error.diagnostics}
                if error.diagnostics.get('reason_kind') == 'provider_rejected':
                    j.put(rid, {'state': 'rejected', 'date': day, 'outcome': entry})
                entries.append(entry)
            client.http.writing = False
        return report({**snapshot, **adapter.profile(client).snapshot}, entries, '游戏中心已有VIP免费券已逐档处理')
    if key == 'gc_native_download_rewards':
        progress = obj(game.call('/clientRequest/downloadWelfare/user/progress'))
        if progress.get('isLoggedIn') is not True:
            raise ActionRequired('下载福利原生会话没有通过本人登录核验，本次跳过。', reason_kind='session_unavailable')
        native = obj(obj(context.credentials.get('scopes')).get('vivo_game_download_welfare'))
        claims = rows(native.get('claims'))
        if native.get('openid') != client.session['openid'] or not claims:
            raise ActionRequired('下载福利需要本人已完成下载的原生领取批次、包名及事件资料；当前会话未携带，未虚报下载或安装。', reason_kind='missing_context')
        entries = []
        import re
        for row in claims[:8]:
            pkg = str(row.get('pkgNames') or '')
            if not re.fullmatch('[A-Za-z0-9_]+(?:\\.[A-Za-z0-9_]+)+', pkg) or type(row.get('batch')) is not int or row['batch'] < 0 or (not row.get('eventId')):
                raise ProtocolError('原生下载福利领取资料不完整，未猜测批次或事件参数。')
            rid = day + ':download:' + pkg + ':' + str(row['batch']) + ':' + str(row['eventId'])
            if j.saved(rid).get('state') == 'confirmed':
                continue
            body = {k: row[k] for k in ('pkgNames', 'batch', 'eventId')}
            receipt = obj(j.submit(rid, lambda body=body: game.call('/clientRequest/downloadWelfare/receive', body, write=True), before=progress))
            succeed = rows(receipt.get('welfareSucceedList'))
            if not any((r.get('pkgName') == pkg and str(r.get('code')) == '0' for r in succeed)):
                raise UnknownWriteResult
            j.put(rid, {'state': 'confirmed', 'date': day, 'receipt': receipt})
            client.http.writing = False
            entries.append({'name': title(row.get('name'), '游戏下载福利'), 'status': 'success', 'message': '本人包名对应的官方领取成功记录已回读'})
        return report(snapshot, entries, '当前已有下载福利已逐项核验；没有虚报安装或下载')
    raise ProtocolError('未注册的原生福利任务。')

def sign_reminder(client, context, snapshot):
    from .vivo import POINTH5
    old = client.pkg
    client.pkg = 'com.vivo.wallet'
    j = Journal(context, 'vivo_main_sign_reminder')

    def read():
        value = obj(obj(client._call(POINTH5, '/api/sign/home.do')).get('data'))
        if type(value.get('allowPush')) is not bool:
            raise ProtocolError('主签到提醒没有返回明确开关状态。')
        return value
    try:
        state = read()
        if state['allowPush']:
            j.confirm('enabled')
            return StepResult('skipped', '官网主签到提醒已开启，无需重复设置', snapshot)
        j.submit('enabled', lambda: client._call(POINTH5, '/api/sign/pushSwitch.do', method='POST', params={'allow': 1}), before=state)
        if not read()['allowPush']:
            raise UnknownWriteResult
        j.confirm('enabled')
        client.http.writing = False
        return StepResult('success', '官网主签到提醒已开启，官方开关状态已回读', snapshot)
    finally:
        client.pkg = old
