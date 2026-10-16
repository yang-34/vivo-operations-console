"""Durable account verification holds and bounded automatic renewal selection."""
import hashlib
from datetime import datetime

from store import db, decrypt, encrypt, now

HOLD_NAMESPACE = 'runtime_account'


def verified_identity(raw, response):
    data=response.get('data') if isinstance(response,dict) else None
    data=data if isinstance(data,dict) else {}
    official=data.get('openId') or data.get('openid')
    placeholder=str(raw.get('openid','')).startswith('wx:')
    if not official:
        if placeholder:raise ValueError('登录态仍使用微信占位标识，官方未返回可核验的 vivo 身份')
        return raw
    if not isinstance(official,str) or official.startswith('wx:') or any(ord(ch)<33 or ord(ch)>126 for ch in official):
        raise ValueError('官方账号身份格式无法核验')
    if not placeholder:
        if raw.get('openid')!=official:raise ValueError('登录态与当前账号身份不一致，已停止执行')
        return raw
    wx=raw.get('_wx_openid') or raw.get('wx_openid')
    if not wx or str(raw['openid'])!='wx:'+str(wx):raise ValueError('微信占位标识与原账号来源不一致，未替换身份')
    if raw.get('unionid') and data.get('unionId') and raw['unionid']!=data['unionId']:
        raise ValueError('官方身份与原账号联合标识不一致，未替换身份')
    return {**raw,'openid':official}


def token_fingerprint(raw):
    return hashlib.sha256(str(raw.get('vivotoken') or '').encode()).hexdigest()


def verification_hold(mode, account_id, raw):
    if not account_id:
        return None
    with db(mode) as conn:
        row = conn.execute("SELECT payload FROM task_journals WHERE account_id=? AND namespace=? AND action_key='verification' AND state='blocked'", (account_id, HOLD_NAMESPACE)).fetchone()
    value = decrypt(row[0]) if row else None
    return value if value and value.get('token_fingerprint') == token_fingerprint(raw) else None


def record_verification_hold(mode, account_id, raw, event):
    diagnostics = event.get('diagnostics') or {}
    if not account_id or diagnostics.get('reason_kind') != 'account_verification':
        return
    value = {'token_fingerprint': token_fingerprint(raw), 'message': event['message'],
             'provider_code': event.get('provider_code'), 'diagnostics': diagnostics}
    with db(mode) as conn:
        conn.execute('INSERT OR REPLACE INTO task_journals VALUES(?,?,?,?,?,?)',
                     (account_id, HOLD_NAMESPACE, 'verification', 'blocked', encrypt(value), now()))


def latest_login_results(conn):
    return {r['account_id']: dict(r) for r in conn.execute("""SELECT r.* FROM runs r
        JOIN (SELECT r.account_id,max(r.id) id FROM runs r JOIN batches b ON b.id=r.batch_id
              WHERE b.profile='login' OR EXISTS(SELECT 1 FROM steps s WHERE s.run_id=r.id AND s.step_key='session.check') GROUP BY r.account_id) latest ON latest.id=r.id""")}


def renewal_candidates(accounts, latest, timestamp):
    from protocol.task_policy import policy
    suspend_failed=policy().get('suspend_failed_renewals',False)
    selected = []
    for account in accounts:
        if account.get('panel_status') == 'offline' or account.get('status') == 'disabled':
            continue
        raw = decrypt(account['credentials'])
        if not raw.get('_pool') or raw.get('profile_kind') == 'app':
            continue
        saved = float(raw.get('saved_at') or 0)
        if not (account['status'] in ('pending_login', 'expired') or not raw.get('vivotoken') or timestamp - saved > 4.75 * 86400):
            continue
        previous = latest.get(account['id']) or {}
        if previous.get('status') == 'failed' and previous.get('finished_at'):
            failed = datetime.fromisoformat(previous['finished_at']).timestamp()
            error = previous.get('error') or ''
            if suspend_failed and saved<=failed:continue
            delay = 86400 if any(word in error for word in ('验证', '账户异常', '手机号授权')) else 3600
            if saved <= failed and timestamp - failed < delay:
                continue
        selected.append(account['id'])
    return selected
