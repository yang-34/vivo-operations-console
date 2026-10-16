# Bundled vivo protocol reference; see source-manifest.json.
"""Game Center's native reservation flow, with owned-reservation readback."""
import json
import time
import uuid
from .added_common import Journal, checked, obj, report, rows, title, yes
from .protocol_common import ActionRequired, ProtocolError, amount
from .sdk import CredentialsExpired, UnknownWriteResult
from .vendor.vivo import jvq, wave
MAIN = 'https://main.gamecenter.vivo.com.cn'
WRITE = 'https://w.gamecenter.vivo.com.cn'

class Reservations:

    def __init__(self, client):
        self.client = client
        self.account = None
        client.http.hosts |= {'main.gamecenter.vivo.com.cn', 'w.gamecenter.vivo.com.cn', 'usrsys.vivo.com.cn'}

    def identity(self):
        c, d = (self.client, self.client.credentials['device'])
        if any((k not in d for k in ('manufacturer', 'model', 'android_ver', 'brand', 'sdk_int', 'density', 'net_type'))):
            raise ActionRequired('游戏预约需要该账号原始设备资料，请补充完整会话包。', reason_kind='missing_context')
        params = {'deviceOrigin': '1' if d['manufacturer'].lower() == 'vivo' else '2', 'oaid': '', 'vaid': '', 'aaid': '', 'imei': '123456789012345', 'ec': '0123456789012345678901234567890123456789012345678901', 'model': d['model'], 'timeStamp': str(int(time.time() * 1000)), 'androidVer': d['android_ver'], 'from': 'com.vivo.game_SysAccountSDK', 'nounce': str(uuid.uuid4()), 'locale': 'zh_CN', 'countryCode': 'CN', 'verCode': 'sysapk_6.2.4.0', 'verCodeInt': '6240', 'sdkVerCode': 'SysAccountSDK_1.2.0.1', 'sdkVerCodeInt': '1201', 'cs': '0', 'isBarrierFree': '0', 'brand': d['brand'], 'openid': c.session['openid'], 'vivotoken': c.session['vivotoken']}
        if int(d['sdk_int']) <= 28:
            params.update({k: d[k] for k in ('imei', 'oaid', 'vaid', 'aaid')})
        params['s'] = wave.md5_wave_sign(list(params.values()))
        response = c.http.request('POST', 'https://usrsys.vivo.com.cn/usrprd/v5/s6/user/show', data=params, headers={'User-Agent': 'Mozilla/5.0 (Windows; U; Windows NT 5.1; zh-CN; rv:1.9.2) Gecko/20100115 Firefox/3.6'})
        try:
            value = response.json()
        except ValueError as exc:
            raise ProtocolError('游戏中心账号资料未完整返回。') from exc
        checked(value, expired=('20002', '200000'), operation='核对游戏中心账号')
        who = obj(value.get('data'))
        if who.get('openid') != c.session['openid'] or not who.get('id'):
            raise CredentialsExpired
        self.account = who

    def call(self, path, fields=None, *, write=False, post=False):
        c, device = (self.client, self.client.credentials['device'])
        model = str(device.get('market_name') or {'V2227A': 'vivo X90 Pro+'}.get(device['model'], device['model']))
        vivo_device = str(device.get('manufacturer', '')).lower().startswith('vivo')
        if vivo_device and 'vivo' not in model.lower():
            model = 'vivo ' + model
        params = {'imei': '' if int(device['sdk_int']) >= 29 else device['imei'], 'model': model, 'u': device.get('ufs_id', ''), 'av': str(device['sdk_int']), 'an': device['android_ver'], 'appVersion': '72151', 'appVersionName': '7.2.15.1', 'from': 'com.vivo.game', 'apppkgName': 'com.vivo.game', 'adrVerName': device['android_ver'], 'vivoModel': '1' if vivo_device else '0', 'oaid': device.get('oaid', ''), 'vaid': device.get('vaid', ''), 'aaid': device.get('aaid', ''), 'pixel': str(device['density']), 'elapsedtime': str(int(time.monotonic() * 1000)), 'nt': str(device['net_type']), 'cs': '0', 'picType': 'webp', 'deviceType': device.get('device_type', 'phone'), 'openid': c.session['openid'], 'userid': c.session['openid'], 'vivotoken': c.session['vivotoken'], 'token': c.session.get('authtoken', ''), 'systoken': c.session['vivotoken'], 'validToken': c.session['vivotoken'], 'bindPhone': '0' if c.session.get('needBindPhone') is True else '1', **(fields or {})}
        if int(device['sdk_int']) >= 26:
            params['sysAccount'] = params['vivoModel']
        if self.account:
            params.update(userName=str(self.account.get('phonenum') or self.account.get('name') or ''), bindPhone='1' if self.account.get('phonenum') else '0')
        if c.session.get('uuid'):
            params['uuid'] = str(c.session['uuid'])
        native_ua = f"Dalvik/2.1.0 (Linux; U; Android {device['android_ver']}; {device['model']})/vivo_game"
        params.update(userAgent=native_ua.removesuffix('/vivo_game'), scoreFlag='1', guestStatus='0', platformVersion='0', platformVersionName='', platApkVer='0', platApkVerName='', recommendModel='0', ageRange='0', minorMode='0', agreePrivacy='1', arcore='1', supportGifIcon='true', clientTraceData='', pid='')
        if params['nt'].lower() == 'wifi':
            params['nt'] = 'WIFI'
        headers = {'User-Agent': native_ua, 'Referer': 'http://www.vivo.com.cn', 'Cookie': ';'.join((f'{k}={params[k]}' for k in ('userid', 'vivotoken', 'token')))}
        c.http.writing = c.http.writing or write
        payload = {'jvq_param': jvq.build_query_jvq(params, pkg_name='com.vivo.game')}
        response = c.http.request('POST' if post or write else 'GET', (WRITE if write else MAIN) + path, headers=headers, **{'data': payload} if post or write else {'params': payload})
        try:
            try:
                value = response.json()
            except ValueError:
                value = json.loads(jvq.decrypt_response(response.text))
            if isinstance(value, dict) and value.get('jvq_response'):
                value = json.loads(jvq.decrypt_response(value['jvq_response']))
        except ValueError as exc:
            raise UnknownWriteResult if c.http.writing else ProtocolError from exc
        code = str(obj(value).get('retcode', obj(value).get('code')))
        if write and code in ('20000', '30560'):
            message = {'20000': '游戏中心拒绝预约参数（20000）；预约尚未建立，请核对原生游戏中心会话。', '30560': '游戏中心提示当前机型不支持预约（30560）；未计作预约完成。'}[code]
            raise ActionRequired(message, provider_code=code, reason_kind='provider_rejected', operation='提交游戏预约')
        checked(value, key='retcode' if 'retcode' in obj(value) else 'code', expired=('20002', '30100'), writing=c.http.writing, operation='游戏预约')
        if yes(obj(value.get('resultInfo')).get('tokenExpired')):
            raise CredentialsExpired
        return value

    def catalogue(self):
        value = self.call('/clientRequest/queryAppointmentList', {'pageIndex': '1', 'pageSize': '20', 'isCollectData': 'true'})
        data = obj(value.get('data'))
        if not isinstance(data.get('appointmentList'), list):
            raise ProtocolError('游戏中心未返回完整的当前预约列表。')
        return [{**obj(r.get('apt')), 'origin': str(value.get('traceId') or '')} for r in rows(data['appointmentList'])]

    def owned(self):
        value = self.call('/clientRequest/queryPersonalAppointmentList', {'pageIndex': '1', 'pageSize': '100'}, post=True)
        if value.get('message') == '暂无预约记录，请先进行游戏预约' and 'data' not in value:
            return []
        data = obj(value.get('data'))
        if not isinstance(data.get('appointmentList'), list):
            raise ProtocolError('本账号预约记录未完整返回。')
        if yes(data.get('hasNext')):
            raise ActionRequired('预约记录超过单页范围，暂不重复预约。', reason_kind='settlement')
        return [obj(r.get('apt')) for r in rows(data['appointmentList'])]

    def check_session(self):
        if not self.client.session.get('authtoken') or not self.client.session.get('uuid'):
            raise ActionRequired('本账号游戏中心原生会话不可用，本次跳过原生预约验收；网站账号的其他免费任务继续。', reason_kind='session_unavailable')

    def appoint(self, game):
        self.check_session()
        trace = obj(game.get('traceData'))
        return self.call('/clientRequest/appointGame', {'id': str(game['id']), 'pkgName': game['pkgName'], 'origin': '', **{k: trace[k] for k in ('game_referrer_server', 'gameps') if k in trace}}, write=True)

def execute(adapter, client, context, snapshot):
    client.pkg = 'com.vivo.game'

    def tasks():
        return [t for t in client.group_list('2') if '预约' in str(t.get('name', ''))]
    selection = tasks()
    api, journal, entries = (Reservations(client), Journal(context, 'vivo_reservations'), [])
    for task in selection:
        tid = str(task.get('id') or '')
        if not tid:
            raise ProtocolError('预约积分任务缺少编号。')
        if yes(task.get('taskComplete')) or amount(task.get('unreceviedPoints', 0)):
            result = adapter.task_group(client, context, 'tasks_2', snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
            entries.extend(result.diagnostics.get('entries', []))
            snapshot = result.snapshot
            continue
        saved = journal.saved(tid)
        api.identity()
        chosen = obj(saved.get('game'))
        owned = api.owned()

        def reserved(game):
            return any((str(r.get('id')) == str(game.get('id')) and r.get('pkgName') == game.get('pkgName') for r in owned))
        if not chosen:
            choices = [g for g in api.catalogue() if g.get('id') and g.get('pkgName') and (g.get('currentStage') == '火爆预约') and (not reserved(g))]
            if not choices:
                raise ActionRequired('当前没有可新增预约的游戏。', reason_kind='unavailable')
            chosen = {k: choices[0][k] for k in ('id', 'pkgName', 'name', 'origin', 'traceData') if k in choices[0]}
        elif saved.get('state') == 'rejected':
            current_game = next((g for g in api.catalogue() if str(g.get('id')) == str(chosen['id']) and g.get('pkgName') == chosen['pkgName']), None)
            if current_game is None:
                raise ActionRequired('此前游戏已退出当前预约目录。', reason_kind='unavailable')
            chosen = {k: current_game[k] for k in ('id', 'pkgName', 'name', 'origin', 'traceData') if k in current_game}
        before = adapter.profile(client).snapshot['points']
        if not reserved(chosen):
            if saved.get('state') in ('pending', 'confirmed'):
                raise ActionRequired('此前预约尚待确认，本轮未重复提交。', reason_kind='settlement')
            api.check_session()
            journal.put(tid, {'state': 'pending', 'game': chosen, 'points_before': before})
            try:
                api.appoint(chosen)
            except ActionRequired as exc:
                if exc.diagnostics.get('reason_kind') == 'provider_rejected':
                    journal.put(tid, {'state': 'rejected', 'game': chosen, 'provider_code': exc.diagnostics.get('provider_code')})
                raise
            owned = api.owned()
            if not reserved(chosen):
                raise UnknownWriteResult
        journal.put(tid, {'state': 'confirmed', 'game': chosen, 'points_before': before})
        client.http.writing = False
        result = adapter.task_group(client, context, 'tasks_2', snapshot, load=lambda: [t for t in tasks() if str(t.get('id')) == tid])
        current = adapter.profile(client).snapshot
        if current['points'] < before:
            raise UnknownWriteResult
        snapshot = {**result.snapshot, **current}
        observed = next((t for t in tasks() if str(t.get('id')) == tid), {})
        complete = yes(observed.get('taskComplete'))
        entries.append({'name': title(task.get('name')), 'status': 'success' if complete else 'action_required', 'message': f"已预约《{title(chosen['name'])}》，个人预约记录已确认；" + (f"积分增加{current['points'] - before}分" if complete else '积分任务尚未达标')})
    return report(snapshot, entries, '游戏实际预约、个人记录与积分任务已核对')
