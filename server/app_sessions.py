"""Encrypted, account-bound App Cookie storage and read-only session verification."""
import json,time,uuid
from flask import Blueprint,jsonify,request
from store import db,decrypt,encrypt,now,account_public
from protocol.app_session import parse_session,assert_not_expired,account_data

blueprint=Blueprint('app_sessions',__name__)


def public(row):
    if not row:return {'status':'missing','cookie_count':0}
    session=decrypt(row['payload']);state=row['status']
    try:assert_not_expired(session)
    except ValueError:state='expired'
    return {'status':state,'cookie_count':len(session['cookies']),'verified_at':row['verified_at'],
            'last_error':row['last_error'],'updated_at':row['updated_at']}


def load(mode,aid,allow_unverified=False):
    with db(mode) as c:row=c.execute('SELECT * FROM app_sessions WHERE account_id=?',(aid,)).fetchone()
    if not row:return None
    if not allow_unverified and row['status']!='verified':raise ValueError('请先核验已导入的官网 App Cookie')
    session=decrypt(row['payload']);assert_not_expired(session)
    return session,row['version']


def import_session(mode,body):
    session=parse_session(body.get('input',''),body.get('user_agent',''))
    account_data(session,{}) # Validate any supplied device information before storing it.
    aid=body.get('account_id');stamp=now()
    with db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute('SELECT * FROM accounts WHERE '+('id=?' if aid else 'external_id=?'),(int(aid) if aid else session['openid'],)).fetchone()
        if row:
            raw=decrypt(row['credentials']);aid=row['id']
            if session['openid'] not in {row['external_id'],raw.get('openid')}:
                raise ValueError('Cookie 属于其他账号，已拒绝绑定；请在对应账号中导入')
            if c.execute('SELECT 1 FROM execution_leases WHERE resource=? AND expires_at>?',('account:'+str(aid),time.time())).fetchone():
                raise ValueError('该账号正在执行，请结束当前操作后再更新 Cookie')
        elif aid:raise ValueError('目标账号不存在')
        elif body.get('create_account') is True:
            raw=account_data(session,{'profile_kind':'app','openid':session['openid']});raw.pop('vivotoken',None)
            from protocol.devices import ensure_device_profile
            raw=ensure_device_profile(raw,session['openid'])
            cursor=c.execute('INSERT INTO accounts(external_id,nickname,source,status,credentials,created_at) VALUES(?,?,?,?,?,?)',
                             (session['openid'],'官网 App 账号','官网 App Cookie','active',encrypt(raw),stamp))
            aid=cursor.lastrowid
        else:raise ValueError('未找到同一账号；如需独立导入，请勾选创建官网 App 账号')
        c.execute('INSERT INTO app_sessions(account_id,payload,version,status,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET payload=excluded.payload,version=excluded.version,status=excluded.status,verified_at=NULL,last_error=\'\',updated_at=excluded.updated_at',
                  (aid,encrypt(session),uuid.uuid4().hex,'unverified',stamp))
    return {'account_id':aid,'status':'unverified','cookie_count':len(session['cookies'])}


@blueprint.post('/api/app-sessions/import')
def import_route():
    from management import mode
    if mode()!='live':raise ValueError('请切换到真实账号环境')
    body=request.get_json() or {}
    if body.get('account_id'):
        from engine import account_lock
        with account_lock(mode(),int(body['account_id'])):return jsonify(import_session(mode(),body))
    return jsonify(import_session(mode(),body))


@blueprint.get('/api/accounts/<int:aid>/app-session')
def status_route(aid):
    from management import mode
    with db(mode()) as c:row=c.execute('SELECT * FROM app_sessions WHERE account_id=?',(aid,)).fetchone()
    return jsonify(public(row))


@blueprint.post('/api/accounts/<int:aid>/app-session/verify')
def verify_route(aid):
    from management import mode
    from engine import account_lock,live_events
    from proxies import lease
    current=mode();loaded=load(current,aid,True)
    if not loaded:raise ValueError('请先导入官网 App Cookie')
    _,version=loaded;result=None
    with account_lock(current,aid),lease(current,aid) as proxy:
        with db(current) as c:row=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        if not row:raise ValueError('账号不存在')
        for event in live_events(current,dict(row),action='app_session_check',proxy=proxy):
            if event['type']=='app_session':result=event
            elif event['type']=='error':
                with db(current) as c:c.execute('UPDATE app_sessions SET status=?,last_error=? WHERE account_id=? AND version=?',('invalid','官网登录态核验未通过，请检查 Cookie 有效期或代理连接',aid,version))
                raise ValueError(event['message'])
        if result is None:raise ValueError('平台未返回登录态核验结果')
        with db(current) as c:
            changed=c.execute('UPDATE app_sessions SET status=\'verified\',verified_at=?,last_error=\'\' WHERE account_id=? AND version=?',(now(),aid,version)).rowcount
            if not changed:raise ValueError('Cookie 已更新，请重新核验最新登录态')
            c.execute('UPDATE accounts SET points=?,points_synced_at=? WHERE id=?',(result['points'],now(),aid))
            if decrypt(row['credentials']).get('profile_kind')=='app' and result.get('nickname'):
                c.execute('UPDATE accounts SET nickname=? WHERE id=?',(result['nickname'],aid))
    return jsonify(status='verified',account_id=aid,points=result['points'],nickname=result.get('nickname',''))


@blueprint.delete('/api/accounts/<int:aid>/app-session')
def delete_route(aid):
    from management import mode
    from engine import account_lock
    with account_lock(mode(),aid),db(mode()) as c:
        if c.execute('SELECT 1 FROM execution_leases WHERE resource=? AND expires_at>?',('account:'+str(aid),time.time())).fetchone():raise ValueError('账号正在执行，请稍后移除')
        c.execute('DELETE FROM app_sessions WHERE account_id=?',(aid,))
    return jsonify(ok=True)
