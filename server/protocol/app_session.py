"""Import owned App sessions; never manufacture authentication or verification cookies."""
from __future__ import annotations
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import unquote,parse_qsl,urlparse

AUTH_NAMES=('vvc_openid','vivo_account_cookie_iqoo_openid','vvc_r','vivo_account_cookie_iqoo_vivotoken')
MALL_HOSTS={'pointh5.vivo.com.cn','pointwap.vivo.com.cn','pointstore.vivo.com.cn'}


def _cookies(value):
    if isinstance(value,str):
        text=value.strip()
        if text[:1] in ('{','['):return _cookies(json.loads(text))
        text=re.sub(r'^cookie\s*:\s*','',text,flags=re.I)
        if '\r' in text or '\n' in text:raise ValueError('Cookie 不能包含换行')
        return [{'name':p.strip().split('=',1)[0],'value':p.strip().split('=',1)[1]} for p in text.split(';') if '=' in p]
    if isinstance(value,dict):return [{'name':k,'value':v} for k,v in value.items()]
    if isinstance(value,list):return value
    raise ValueError('请提供 Cookie 字符串、JSON Cookie 列表或 HAR')


def parse_session(value,user_agent=''):
    if isinstance(value,str) and value.strip()[:1] in ('{','['):value=json.loads(value)
    if isinstance(value,dict) and 'log' in value:
        candidates=[]
        for entry in value.get('log',{}).get('entries',[]):
            r=entry.get('request',{});u=urlparse(r.get('url',''))
            if u.hostname not in MALL_HOSTS:continue
            headers={h.get('name','').lower():h.get('value','') for h in r.get('headers',[])}
            # The HAR cookie list may be complete while the Cookie header is partial.
            cookies={c['name']:c for c in _cookies(r.get('cookies',[])) if isinstance(c,dict) and c.get('name')}
            for c in _cookies(headers.get('cookie','')):cookies.setdefault(c['name'],c)
            if not any(cookies.get(k,{}).get('value') for k in AUTH_NAMES[:2]):continue
            candidates.append((entry.get('startedDateTime',''),{'cookies':list(cookies.values()),'user_agent':headers.get('user-agent','')}))
        if not candidates:raise ValueError('HAR 中没有官网商城的账号 Cookie')
        identities={unquote(str(next((c['value'] for c in v['cookies'] if c['name'] in AUTH_NAMES[:2] and c.get('value')),''))) for _,v in candidates}
        if len(identities)!=1:raise ValueError('HAR 包含多个账号，请单独导出目标账号的 Cookie')
        value=max(candidates,key=lambda x:x[0])[1]
    if isinstance(value,dict) and 'cookies' in value:
        user_agent=user_agent or value.get('user_agent') or value.get('userAgent') or ''
        value=value['cookies']
    parsed={}
    for item in _cookies(value):
        if not isinstance(item,dict):raise ValueError('Cookie 列表格式错误')
        name=str(item.get('name') or '');content=str(item.get('value') or '')
        if not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+',name) or any(ord(ch)<32 or ord(ch)>126 or ch==';' for ch in content):
            raise ValueError('Cookie 名称或值含非法字符')
        if len(name)>120 or len(content)>12000:raise ValueError('Cookie 字段过长')
        domain=str(item.get('domain') or '').lower().lstrip('.')
        if domain and domain!='vivo.com.cn' and not domain.endswith('.vivo.com.cn'):raise ValueError('Cookie 域名必须属于 vivo.com.cn')
        if name in parsed and parsed[name]['value']!=content:raise ValueError('Cookie 中存在冲突的同名字段')
        expires=item.get('expires',item.get('expirationDate'))
        if isinstance(expires,str):
            try:expires=datetime.fromisoformat(expires.replace('Z','+00:00')).timestamp()
            except ValueError:raise ValueError('Cookie 过期时间格式无效') from None
        if expires is not None:
            try:expires=float(expires)
            except (ValueError,TypeError):raise ValueError('Cookie 过期时间格式无效') from None
            if not math.isfinite(expires):raise ValueError('Cookie 过期时间格式无效')
        parsed[name]={'name':name,'value':content,'expires_at':expires if expires and expires>0 else None}
    if not 1<=len(parsed)<=200 or sum(len(x['value']) for x in parsed.values())>64000:raise ValueError('Cookie 数量或总长度超出限制')
    ids={unquote(parsed[k]['value']) for k in AUTH_NAMES[:2] if k in parsed and parsed[k]['value']}
    tokens={parsed[k]['value'] for k in AUTH_NAMES[2:] if k in parsed and parsed[k]['value']}
    if len(ids)!=1 or len(tokens)!=1:raise ValueError('需要一致的官网账号标识和登录 token，不能拼接多个账号的 Cookie')
    if not isinstance(user_agent,str) or len(user_agent)>8192 or any(ord(ch)<32 or ord(ch)>126 for ch in user_agent):raise ValueError('User-Agent 格式无效')
    result={'openid':ids.pop(),'cookies':list(parsed.values()),'user_agent':user_agent}
    assert_not_expired(result)
    return result


def assert_not_expired(session):
    if any(c['name'] in AUTH_NAMES and c.get('expires_at') and c['expires_at']<=time.time() for c in session['cookies']):
        raise ValueError('官网 App 登录 Cookie 已过期，请重新导出')


def cookie_header(session):
    assert_not_expired(session)
    return '; '.join(c['name']+'='+c['value'] for c in session['cookies'] if not c.get('expires_at') or c['expires_at']>time.time())


def account_data(session,raw):
    """Use the App's identity and original device, leaving the mini-app credentials intact."""
    cookies={c['name']:c['value'] for c in session['cookies']}
    device={}
    ua=session['user_agent'];match=re.search(r'Android ([\d.]+); ([^;)]+)',ua)
    if match:
        model=match[2].split(' Build/')[0].strip()
        if not re.fullmatch(r'[Vv][0-9]{4}[A-Za-z]*',model):raise ValueError('官网 App 会话必须使用 vivo 设备')
        device.update(brand='vivo',manufacturer='vivo',android_ver=match[1],model=model)
        device['sdk_int']=int(cookies.get('vvc_av') or int(float(match[1]))+20)
        prefix=ua.split(' Mozilla/',1)[0]
        params=dict(parse_qsl(prefix.split('?',1)[1])) if '?' in prefix else {}
        for key in ('density','screensize'):
            if params.get(key):device[key]=params[key]
        build=re.search(r' Build/([^;)]+)',ua)
        if build:device['build_id']=build[1]
    for key,target in (('vvc_imei','imei'),('vvc_oaid','oaid'),('vvc_vaid','vaid'),('vvc_aaid','aaid')):
        if cookies.get(key):device[target]=unquote(cookies[key])
    token=next(cookies[k] for k in AUTH_NAMES[2:] if cookies.get(k))
    return {**raw,'openid':session['openid'],'vivotoken':token,**({'device':device,'protocol_device':{}} if match else {})}


def configure_api(api,session):
    api.app_session=session
    if session['user_agent']:
        api.ua_h5=session['user_agent'];api.ua_wap=session['user_agent'];api.ua_space=session['user_agent']
        api.s.headers['User-Agent']=session['user_agent']


def read_profile(api):
    response=api._pm_get('/api/mall/v5/home/header.do')
    if not api.ok(response) or response.get('login') not in (True,1):raise ValueError('官网 App 登录态无效或已过期，请重新登录后导入 Cookie')
    data=response.get('data') or {};points=data.get('points')
    if isinstance(points,bool) or not isinstance(points,(int,float)) or not 0<=points<=10**12 or int(points)!=points:
        raise ValueError('官网 App 未返回明确的可用积分')
    for key in ('openid','openId','userOpenId'):
        if data.get(key) and str(data[key])!=api.app_session['openid']:raise ValueError('平台返回的账号与导入 Cookie 不一致')
    return {'points':int(points),'nickname':str(data.get('userName') or '')[:120]}
