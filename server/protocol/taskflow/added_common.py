import hashlib,re,json
from types import SimpleNamespace
from store import db,encrypt,decrypt,now
from .compat import timezone
from .sdk import CredentialsExpired,Identity,StepResult,UnknownWriteResult
from .protocol_common import ProtocolError,ActionRequired
def obj(value):return value if isinstance(value,dict) else {}
def rows(value):return [r for r in value if isinstance(r,dict)] if isinstance(value,list) else []
def yes(value):return value is True or str(value).lower() in ('1','true','y')
def title(value,fallback='活动'):
    value=re.sub(r'1[3-9]\d{9}','[手机号]',str(value or fallback))
    return fallback if 'http' in value.lower() or re.search('[A-Za-z0-9_=-]{40,}',value) else value[:120]
def identity(identifier,snapshot,prefix):return Identity(str(identifier),prefix+':'+hashlib.sha256(str(identifier).encode()).hexdigest()[:8],snapshot)
def checked(value,*,key='code',good=('0',),expired=(),operation='查询活动',writing=False):
    if not isinstance(value,dict) or key not in value:raise UnknownWriteResult() if writing else ProtocolError('平台响应缺少明确业务状态')
    code=str(value[key])
    if code in expired:raise CredentialsExpired()
    if code not in good:
        message=title(value.get('message') or value.get('msg') or value.get('retmsg'),'平台未返回明确原因')
        raise ActionRequired(message,provider_code=code,provider_message=message,operation=operation,reason_kind='provider_rejected',provider_result_status='action_required' if any(s in message for s in ('资格','先完成','未满足','未开通','仅限','上限','验证码','滑块','实名认证','身份认证')) else 'failed')
    return value
class Journal:
    def __init__(self,context,namespace):
        self.context=context;self.namespace='_taskflow_'+namespace
        with db(context.mode) as c:records=c.execute('SELECT action_key,payload FROM task_journals WHERE account_id=? AND namespace=?',(context.account_id,self.namespace)).fetchall()
        scopes=context.credentials.setdefault('scopes',{})
        scopes[self.namespace]={**obj(scopes.get(self.namespace)),**{row['action_key']:decrypt(row['payload']) for row in records}}
    def saved(self,key):
        with db(self.context.mode) as c:row=c.execute('SELECT payload FROM task_journals WHERE account_id=? AND namespace=? AND action_key=?',(self.context.account_id,self.namespace,str(key))).fetchone()
        value=decrypt(row[0]) if row else {}
        if str(value.get('provider_code'))=='21057':
            value.update(provider_result_status='action_required',provider_message='平台要求滑块验证码，需本人在官方客户端完成验证或领取')
        return value
    def put(self,key,value):
        with db(self.context.mode) as c:c.execute('INSERT OR REPLACE INTO task_journals VALUES(?,?,?,?,?,?)',(self.context.account_id,self.namespace,str(key),value.get('state','pending'),encrypt(value),now()))
        scopes=self.context.credentials.setdefault('scopes',{})
        scopes.setdefault(self.namespace,{})[str(key)]=value
        self.context.persist_scopes(scopes)
    def submit(self,key,write,*,before=None):
        if self.saved(key).get('state')=='pending':raise ActionRequired('此前操作尚待确认，本轮不重复提交',reason_kind='settlement')
        self.put(key,{'state':'pending','date':timezone.localdate().isoformat(),'before':before})
        try:return write()
        except ActionRequired as error:
            if error.diagnostics.get('reason_kind')=='provider_rejected':self.put(key,{'state':'rejected','date':timezone.localdate().isoformat(),**{k:v for k,v in error.diagnostics.items() if k.startswith('provider_')}})
            raise
    def confirm(self,key):self.put(key,{'state':'confirmed','date':timezone.localdate().isoformat()})
class AddedAdapter:pass
class ClientLifecycle:
    def close(self):self.http.close()
    def updates(self):return {}
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
def report(snapshot,entries,message='已核验任务状态',*,completed=0):
    entries=[{**e,'status':'failed'} if e.get('provider_result_status')=='failed' else e for e in entries]
    state='failed' if any(e.get('status')=='failed' for e in entries) else 'action_required' if any(e.get('status')=='action_required' for e in entries) else 'success' if completed or any(e.get('status')=='success' for e in entries) else 'skipped'
    return StepResult(state,message,snapshot,diagnostics={'entries':entries})
