import re
from contextvars import ContextVar
from decimal import Decimal
from urllib.parse import urlparse
from .sdk import PlatformError

CURRENT=ContextVar('vivo_protocol_context',default=None)
class ProtocolError(PlatformError):pass
class ActionRequired(ProtocolError):pass
class ReadStageError(ProtocolError):pass
def obj(value):return value if isinstance(value,dict) else {}
def decimal_amount(value):
    if value is None or isinstance(value,bool):raise ProtocolError('平台未提供明确价格或余额')
    value=Decimal(str(value))
    if not value.is_finite() or not 0<=value<=10**12:raise ProtocolError('价格或余额超出范围')
    return value
def amount(value):return int(decimal_amount(0 if value is None else value))
def require_header(value,name='UA',limit=8192):
    if not isinstance(value,str) or len(value)>limit or not value or any(ord(ch)<32 or ord(ch)>126 for ch in value):raise ProtocolError('请求头格式无效：'+name)
    return value
class GuardedSession:
    def __init__(self,hosts,ensure_allowed=lambda:None):
        self.hosts=set(hosts);self.ensure_allowed=ensure_allowed;self.writing=False
        context=CURRENT.get()
        if context is None:raise RuntimeError('缺少账号执行上下文')
        self.transport=context.transport
    def request(self,method,url,**kwargs):
        visiting=bool(kwargs.pop('review_redirect',False))
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname not in self.hosts or parsed.username or parsed.password or parsed.port not in (None,443):raise ProtocolError('请求目标不在已核验协议范围')
        self.ensure_allowed()
        # Redirects must not carry account cookies to another host.
        kwargs.setdefault('allow_redirects',False)
        kwargs.setdefault('timeout',20)
        kwargs['allow_non_json']=visiting or parsed.hostname.endswith('gamecenter.vivo.com.cn') or parsed.hostname in ('vivo.yidianzixun.com','eden.vivo.com.cn')
        return self.transport.request(method,url,**kwargs)
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)
    def close(self):pass
