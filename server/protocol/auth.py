"""Application-bao panel authentication through an explicitly selected transport."""
from .common import MPC,VIVO_APPID
from .transport import AccountStop


def renew(raw,session,allow_http=False,api_key=''):
    base=str(raw.get('_pool') or '').rstrip('/')
    if not base:raise AccountStop('该账号没有来源面板，无法自动续期')
    if raw.get('_panel_status')=='offline':raise AccountStop('来源面板已标记本账号离线，当前无法取码；待账号上线后再续期',reason_kind='panel_offline')
    if base.lower().startswith('http://') and not allow_http:
        raise AccountStop('应用宝 HTTP 取码请求未获得授权')
    ref=raw.get('_wx_openid') or raw.get('wx_openid') or raw['openid'].removeprefix('wx:')
    headers={'apiKey':api_key} if api_key else {}
    def finish(data):
        previous=str(raw.get('openid') or '')
        if previous and not previous.startswith('wx:') and str(data.get('openid'))!=previous:
            raise AccountStop('面板返回了不同账号的登录态，已拒绝替换，请核对账号来源')
        return {**raw,**data,'phone':data.get('phonenum') or raw.get('phone','')}
    def panel(path):
        response=session.post(base+path,json={'ref':ref,'app_id':VIVO_APPID},headers=headers,timeout=15)
        if getattr(response,'status_code',200)>=400:
            status=response.status_code
            raise AccountStop(f'应用宝取码返回 HTTP {status}，未取得有效登录码；等待面板或账号恢复',provider_code=f'HTTP_{status}',reason_kind='panel_http',endpoint=path,http_status=status)
        response.raise_for_status();data=response.json()
        if str(data.get('code'))!='0':raise AccountStop('应用宝面板取码失败，请检查面板与账号在线状态')
        return str(((data.get('data') or {}).get('result') or {}).get('code') or '')
    code=panel('/wxapp/getCode')
    if not code:raise AccountStop('应用宝未返回有效登录码')
    request_headers={'content-type':'application/json;charset=UTF-8','wxAppId':VIVO_APPID}
    data=(session.post(MPC+'/api/wx/login',json={'code':code},headers=request_headers,timeout=15).json().get('data') or {})
    if data.get('openid') and str(data.get('needBindPhone'))=='0':
        return finish(data)
    if str(data.get('needBindPhone'))!='1' or not data.get('vivotoken'):
        raise AccountStop('登录未成功，可能需要账号验证')
    phone_code=panel('/wxapp/getPhoneNumber')
    if not phone_code:raise AccountStop('应用宝未返回手机号授权码')
    request_headers['vivo-token']=data['vivotoken']
    phone=(session.post(MPC+'/api/wx/decryptPhoneV2',json={'phoneNumberCode':phone_code},headers=request_headers,timeout=15).json().get('data') or {})
    body={'randomNum':data.get('randomNum'),'encryptMobile':phone.get('encryptMobile'),'mobile':None,'stoken':phone.get('stoken'),'mobileCode':None,'step':0}
    result=(session.post(MPC+'/api/wx/bindAndLoginV2',json=body,headers=request_headers,timeout=15).json().get('data') or {})
    if not result.get('openid') or not result.get('vivotoken'):
        raise AccountStop('登录需要进一步验证，已保留账号等待处理')
    return finish(result)
