"""Reusable connections, proxy-scoped pacing, bounded timeouts and request metrics."""
from __future__ import annotations
import json
import time
import random
from contextlib import contextmanager
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter


class AccountStop(BaseException):
    def __init__(self,message,provider_code=None,network_failure=False,**diagnostics):
        super().__init__(message);self.provider_code=provider_code;self.network_failure=network_failure;self.diagnostics=diagnostics


class StepTimeout(BaseException):
    pass


# Some GET endpoints claim rewards or change state. Retry only verified query contracts.
READ_PATHS={
    '/api/wx/user/userInfo','/home/page/v5/queryInfo.do','/home/page/v5/homePageZoneInfo.do','/api/applet/xp/userInfo.do',
    '/sdk/config/action/list','/api/task/list.do','/api/task/listByType.do','/api/task/detail.do',
    '/api/task/ec/taskList.do','/api/sign/home.do','/api/sign/info.do','/api/sign/capital/pointInfo.do',
    '/api/mall/v5/home/header.do','/api/mall/v5/home/categories.do','/api/mall/v5/ec/categories.do',
    '/api/mall/v5/home/items.do','/wukongtopic/site/online/query','/wukongtopic/site/config/query',
    '/wukongapc/task/list','/wukongapc/signin/user/info','/wukongapc/lottery/user/available/query',
    '/wukongapc/carveupLuck/lucky/available',
}


class Transport(requests.Session):
    def __init__(self,proxy=None,gap=.6):
        super().__init__()
        self.trust_env=False
        self.verify=True
        self.proxies.update(proxy or {})
        self.mount('https://',HTTPAdapter(pool_connections=32,pool_maxsize=2,max_retries=0))
        self.mount('http://',HTTPAdapter(pool_connections=32,pool_maxsize=2,max_retries=0))
        self.gap=gap
        self.last_request=0.
        self.requests_count=0
        self.network_seconds=0.
        self.failures=0
        self.deadline=float('inf')
        self.cache_get=None
        self.cache_put=None
        self.guard=None
        self.progress=None
        self.observations=[]
        self.read_retry_count=2
        self.retry_backoff=.5
        self.retries_count=0
        self.unsafe_sent=False

    def reset_account(self):
        self.cookies.clear()
        self.headers.clear()
        self.headers.update(requests.utils.default_headers())
        self.auth=None
        self.failures=0
        self.observations=[]
        self.unsafe_sent=False

    def request(self,method,url,**kwargs):
        allow_non_json=bool(kwargs.pop('allow_non_json',False))
        if self.guard:self.guard()
        if time.monotonic()>self.deadline:
            raise StepTimeout('当前步骤超过执行时限，已保留断点并继续其它步骤')
        # Only this public configuration response is shared across accounts.
        cache_key=None
        if urlparse(url).path=='/sdk/config/action/list':
            data=kwargs.get('json') or kwargs.get('params') or {}
            cache_key='sdk-actions:v2:'+str(data.get('pkgName',''))
            cached=self.cache_get(cache_key) if self.cache_get else None
            if cached is not None:
                response=requests.Response();response.status_code=200
                response._content=json.dumps(cached).encode();response.headers['Content-Type']='application/json'
                return response
        kwargs['verify']=True
        # Every account business request uses the assigned proxy; no environment fallback.
        kwargs['proxies']=self.proxies
        requested=kwargs.get('timeout',15)
        requested=requested[-1] if isinstance(requested,tuple) else requested
        kwargs['timeout']=(5,min(15,float(requested or 15)))
        path=urlparse(url).path
        attempts=1+max(0,min(3,self.read_retry_count)) if path in READ_PATHS else 1
        for attempt in range(attempts):
            delay=max(0.,self.gap-(time.monotonic()-self.last_request))
            if attempt:delay=max(delay,self.retry_backoff*(2**(attempt-1))*random.uniform(.85,1.15))
            if time.monotonic()+delay>=self.deadline:raise StepTimeout('步骤时限不足，保留断点等待后续核验')
            time.sleep(delay)
            if self.guard:self.guard()
            self.last_request=time.monotonic()
            started=time.monotonic();self.requests_count+=1
            if self.progress:
                try:self.progress()
                except Exception:pass
            try:
                if path not in READ_PATHS:self.unsafe_sent=True
                response=super().request(method,url,**kwargs)
                self.failures=0
            except requests.RequestException as exc:
                self.failures+=1
                if self.failures>=3:
                    self.observations.append({'endpoint':path,'code':'network','message':type(exc).__name__,'status':'failed'})
                    raise AccountStop('代理连续三次连接失败，停止本次执行并等待安全恢复',network_failure=True)
                if isinstance(exc,(requests.Timeout,requests.ConnectionError)) and attempt+1<attempts:
                    self.retries_count+=1;continue
                self.observations.append({'endpoint':path,'code':'network','message':type(exc).__name__,'status':'failed'})
                raise
            finally:self.network_seconds+=time.monotonic()-started
            if response.status_code in (502,503,504) and attempt+1<attempts:
                self.retries_count+=1;response.close();continue
            break
        if response.status_code in (403,429):
            raise AccountStop(f'平台返回 HTTP {response.status_code}，停止该账号')
        if response.status_code>=400:
            self.observations.append({'endpoint':path,'code':f'HTTP_{response.status_code}','message':'HTTP 请求未成功，保留失败状态','status':'failed'})
        try:body=response.json()
        except ValueError:body=None
        if isinstance(body,(dict,list)):
            from .task_policy import filter_response
            filtered=filter_response(path,body)
            if filtered is not body:
                body=filtered;response._content=json.dumps(body,ensure_ascii=False).encode('utf-8');response.encoding='utf-8'
                response.headers.pop('Content-Length',None)
        if isinstance(body,dict):
            code=body.get('retcode',body.get('code'))
            accepted=('0','200','10002','10003') if '/sign/' in urlparse(url).path or '/signin/' in urlparse(url).path else ('0','200')
            if urlparse(url).path.endswith('/thread/like'):accepted+=('16002',)
            if urlparse(url).path.endswith('/thread/favorite'):accepted+=('16016',)
            if code is not None and str(code) not in accepted:
                message=str(body.get('message') or body.get('msg') or '')[:300]
                unavailable=str(code) in ('12706','14001','20041') or str(code)=='20001' and '活动不存在' in message
                self.observations.append({'endpoint':urlparse(url).path,'code':code,'message':message,'status':'skipped' if unavailable else 'failed'})
        elif not isinstance(body,list) and not allow_non_json:
            self.observations.append({'endpoint':urlparse(url).path,'code':'invalid_response','message':'接口未返回有效 JSON 对象','status':'failed'})
        if isinstance(body,dict) and str(body.get('code',body.get('retcode'))) in ('1018','1050'):
            provider_message=str(body.get('message') or body.get('msg') or '')[:200]
            raise AccountStop('平台要求账号验证，停止该账号；不更换设备或出口',str(body.get('code',body.get('retcode'))),reason_kind='account_verification',provider_message=provider_message,endpoint=path)
        if cache_key and isinstance(body,dict) and str(body.get('code',body.get('retcode'))) in ('0','200') and self.cache_put:
            actions=(body.get('data') or {}).get('actions') or []
            self.cache_put(cache_key,{'code':0,'data':{'actions':[{k:v for k,v in a.items() if k in ('actionId','actionName','name','description','taskId','scene')} for a in actions if a.get('actionId')]}})
        return response

    @contextmanager
    def step_budget(self,seconds):
        previous=self.deadline;self.deadline=time.monotonic()+seconds
        try:yield
        finally:self.deadline=previous
