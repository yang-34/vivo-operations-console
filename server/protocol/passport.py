"""Read-only passport research helpers, based on the supplied App SDK sources.

No login, SMS, password submission, or token-refresh mutation is sent here.
"""
from __future__ import annotations
import re
import time
import uuid
from urllib.parse import unquote
from .taskflow.vendor.vivo.wave import md5_wave_sign

HOST='https://usrsys.vivo.com.cn'
VALIDATE='/usrlg/sdk/validateToken'
USER_SHOW='/usrprd/v5/s6/user/show'
ACCOUNT_UA='Mozilla/5.0 (Windows; U; Windows NT 5.1; zh-CN; rv:1.9.2) Gecko/20100115 Firefox/3.6'


def credentials(session):
    cookies={c['name']:unquote(c['value']) for c in session['cookies']}
    identity=cookies.get('vivo_account_cookie_iqoo_openid') or cookies.get('vvc_openid')
    token=cookies.get('vivo_account_cookie_iqoo_vivotoken') or cookies.get('vvc_r')
    if not identity or identity!=session['openid'] or not token:raise ValueError('缺少同账号的真实 App 登录凭证')
    return identity,token,cookies


def sign(params):
    data={k:str(v) for k,v in params.items() if k!='s'}
    data['s']=md5_wave_sign(list(data.values()))
    return data


def validate_params(session,device):
    identity,token,cookies=credentials(session)
    params={'clientId':'5','openid':identity,'vivotoken':token,'scene':'SceneAccountVerify12345678901234',
            'timeStamp':str(int(time.time()*1000)),'locale':'zh_CN','countryCode':'CN','regionCode':'CN',
            'appPkgName':'com.vivo.space','from':'com.vivo.space_SysAccountSDK',
            'appVersionName':cookies.get('vvc_app_name') or '13.4.1.5',
            'appVersionCode':cookies.get('vvc_app_version') or '13415',
            'sdkVerCode':'2.2.1.2','sdkVerCodeInt':'2212','apkVerCodeInt':'-1','model':device['model']}
    # D0.a() adds hardware identifiers only when the identifier callback supplies them.
    for source,target in (('vvc_imei','imei'),('vvc_vaid','vaid')):
        if cookies.get(source):params[target]=cookies[source]
    return sign(params)


def user_show_params(session,device):
    identity,token,_=credentials(session)
    params={'openid':identity,'vivotoken':token,'deviceOrigin':'1','model':device['model'],
            'androidVer':str(device['android_ver']),'imei':'123456789012345',
            'ec':'0123456789012345678901234567890123456789012345678901',
            'from':'com.vivo.space_SysAccountSDK','timeStamp':str(int(time.time()*1000)),
            'nounce':str(uuid.uuid4()),'locale':'zh_CN','countryCode':'CN','brand':'vivo',
            'verCode':'sysapk_6.2.4.0','verCodeInt':'6240','apkVerCodeInt':'-1',
            'sdkVerCode':'SysAccountSDK_1.2.0.1','sdkVerCodeInt':'1201','cs':'0','isBarrierFree':'0'}
    if int(device['sdk_int'])>=29:params.update(oaid='',vaid='',aaid='')
    return sign(params)


def read_request(transport,path,params):
    if path not in (VALIDATE,USER_SHOW):raise ValueError('研究工具只允许 token 校验和用户信息查询')
    response=transport.post(HOST+path,data=params,headers={'Charset':'UTF-8','accept-charset':'UTF-8',
        'User-Agent':ACCOUNT_UA,'Content-Type':'application/x-www-form-urlencoded;charset=utf-8','Cookie':''},
        allow_redirects=False,timeout=20)
    response.raise_for_status();data=response.json()
    if not isinstance(data,dict):raise ValueError('账号接口未返回 JSON 对象')
    return data


def core_session(session):
    """oi/t.java derives these three Cookie fields from an already authenticated pair.

    This preserves existing credentials; it does not log in or renew them.
    """
    identity,token,_=credentials(session)
    expiries=[c['expires_at'] for c in session['cookies'] if c['name'] in ('vvc_openid','vvc_r','vivo_account_cookie_iqoo_openid','vivo_account_cookie_iqoo_vivotoken') and c.get('expires_at')]
    expiry=min(expiries) if expiries else None
    return {**session,'cookies':[{'name':key,'value':value,'expires_at':expiry} for key,value in (
        ('vivo_account_cookie_iqoo_openid',identity),('vivo_account_cookie_iqoo_checksum',token),
        ('vivo_account_cookie_iqoo_vivotoken',token))]}


def login_readiness(raw,session):
    _,_,cookies=credentials(session)
    phone=str(raw.get('phone') or '')
    full_phone=bool(re.fullmatch(r'1[3-9][0-9]{9}',phone))
    context=raw.get('passport_context') or {}
    return {'native_identity_present':True,'native_token_present':True,
            'full_phone_present':full_phone,'password_present':bool(raw.get('passport_password')),
            'sms_context_present':bool(context.get('randomNum')),'sms_code_present':False,
            'relay_context_present':bool(context.get('relay_random_num')),
            'relay_confirmed':context.get('relay_confirmed') is True,
            'device_sdk_bridge_configured':False,
            'sms_start_missing':[] if full_phone else ['phone'],
            'sms_finish_missing':(['phone'] if not full_phone else [])+([] if context.get('randomNum') else ['p1.randomNum'])+['sms.code'],
            'password_login_missing':(['account'] if not full_phone and not raw.get('passport_account') else [])+([] if raw.get('passport_password') else ['password']),
            'fast_login_missing':(['account'] if not full_phone else [])+([] if context.get('relay_random_num') else ['relay.randomNum'])+([] if context.get('relay_confirmed') is True else ['relay_confirmation'])}
