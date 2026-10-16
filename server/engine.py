from __future__ import annotations
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib

from adapter import import_accounts
from store import DATA, ROOT, db, decrypt, encrypt, now, today

MANAGER_LOCK = threading.RLock()
ACTIVE = set()
ACCOUNT_LOCKS = {}


def account_lock(mode, account_id):
    with MANAGER_LOCK:
        return ACCOUNT_LOCKS.setdefault((mode,account_id),threading.Lock())


def live_events(mode,account,action='tasks',**extra):
    raw=decrypt(account.get('credentials',''))
    if action=='tasks' and extra.get('profile') not in ('sync','login','app_check'):
        from execution_policy import verification_hold
        hold=verification_hold(mode,account.get('id'),raw)
        if hold:
            yield {'type':'error','message':hold['message'],'provider_code':hold.get('provider_code'),'diagnostics':hold.get('diagnostics',{}),'retryable':False}
            return
    with db(mode) as c:
        settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
    if mode=='live' and settings.get('proxy_required') and not settings.get('public_proxy_auth_approved'):
        raise ValueError('等待真实账号代理用途授权')
    if 'proxy' not in extra:
        from proxies import lease,service_proxy
        with (service_proxy(mode) if action=='pools' else lease(mode,account['id'])) as proxy:
            yield from live_events(mode,account,action,proxy=proxy,**extra)
        return
    payload={'mode':mode,'account_id':account.get('id'),'account':raw,'action':action,
      'proxy_required':settings.get('proxy_required',True),'allow_http_panels':settings.get('allow_http_panels',False),
      'request_gap':settings.get('request_gap',.6),'step_timeout_seconds':settings.get('step_timeout_seconds',180),
      'read_retry_count':settings.get('read_retry_count',2),'retry_backoff':settings.get('retry_backoff',.5),**extra}
    payload['account'].setdefault('_wx_nick',account.get('nickname',''))
    payload['account']['_panel_status']=account.get('panel_status','unknown')
    if payload['account'].get('_pool'):
        fingerprint=hashlib.sha256(payload['account']['_pool'].rstrip('/').encode()).hexdigest()
        with db(mode) as c:source=c.execute('SELECT api_key,enabled FROM panel_sources WHERE fingerprint=?',(fingerprint,)).fetchone()
        if source and not source['enabled']:raise ValueError('账号来源面板已停用')
        if source and source['api_key']:payload['panel_api_key']=decrypt(source['api_key']).get('key','')
    if action=='pools':
        from native_panels import iter_events
        with db(mode) as c:
            payload['panel_sources']=[{'url':decrypt(r['url'])['url'],'label':r['label'],'api_key':decrypt(r['api_key']).get('key','') if r['api_key'] else ''} for r in c.execute('SELECT * FROM panel_sources WHERE enabled=1 ORDER BY label') if payload.get('panel_source_ids') is None or r['fingerprint'] in payload['panel_source_ids']]
    else:
        from protocol.runner import iter_events
    for event in iter_events(payload):
        if event.get('type')=='error' and event.get('network_failure'):
            url=(payload.get('proxy') or {}).get('https')
            if url:
                with db(mode) as c:c.execute("UPDATE proxies SET status='failed',last_error='任务中连续连接失败，等待重新检测',checked_at=? WHERE fingerprint=?",(now(),hashlib.sha256(url.encode()).hexdigest()))
        yield event


def recover(mode):
    with db(mode) as c:
        c.execute("UPDATE runs SET status='pending',error='服务重启，等待恢复' WHERE status='running' AND id NOT IN (SELECT run_id FROM execution_leases WHERE expires_at>?)",(time.time(),))
        c.execute("UPDATE batches SET status='paused' WHERE status IN ('running','stopping')")


def create_batch(mode, profile='full', ids=None, retry_batch=None, concurrency=20,start_paused=False,validation=False,only_steps=None):
    if profile not in ('full','daily','sync','login','app_check'):
        raise ValueError('未知任务方案')
    concurrency = max(1,min(256,int(concurrency)))
    if only_steps is not None:
        if profile!='full' or not isinstance(only_steps,list) or not only_steps or len(only_steps)>100 or any(not isinstance(key,str) for key in only_steps):
            raise ValueError('定向任务须指定完整任务方案中的非空步骤清单')
        from protocol.api import Account,VivoApi
        from protocol.runner import step_plan
        api=VivoApi(Account({'openid':'offline-validation','vivotoken':'offline'}))
        try:allowed={key for key,_,_ in step_plan(api,'full')}
        finally:api.s.close()
        if set(only_steps)-allowed:raise ValueError('定向任务包含未知或未自动启用的步骤')
        only_steps=list(dict.fromkeys(only_steps))
    with MANAGER_LOCK, db(mode) as c:
        if c.execute("SELECT 1 FROM batches WHERE status IN ('running','stopping')" if validation else "SELECT 1 FROM batches WHERE status IN ('running','paused','stopping')").fetchone():
            raise ValueError('已有未结束批次，请先完成或停止该批次')
        if retry_batch:
            rows = c.execute("SELECT account_id id FROM runs WHERE batch_id=? AND status IN ('failed','partial')",(retry_batch,)).fetchall()
        elif ids is not None:
            if not isinstance(ids,list) or not ids:
                raise ValueError('请至少选择一个账号')
            wanted = {int(x) for x in ids}
            rows = [r for r in c.execute('SELECT id FROM accounts') if r['id'] in wanted]
            if len(rows)!=len(wanted):
                raise ValueError('选中的部分账号不存在，请刷新列表')
        else:
            # Intentionally no LIMIT: paging the UI must never limit batch coverage.
            rows = c.execute('SELECT id FROM accounts ORDER BY id').fetchall()
        if not rows:
            raise ValueError('没有可执行账号，请先导入账号')
        bid = 'B'+uuid.uuid4().hex[:12].upper()
        name = {'full':'全量任务执行','daily':'每日签到任务','sync':'账号积分同步','login':'小程序登录态获取 / 续期','app_check':'官网 App 会话核验'}[profile]
        c.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,NULL)',(bid,name,profile,'paused' if start_paused else 'running',len(rows),concurrency,now()))
        scope='validation' if validation else 'retry' if retry_batch else 'selected' if ids is not None else 'all'
        c.execute('INSERT INTO batch_options VALUES(?,?)',(bid,scope))
        if only_steps is not None:c.execute('INSERT INTO batch_steps VALUES(?,?)',(bid,json.dumps(only_steps)))
        c.executemany('INSERT INTO runs(batch_id,account_id) VALUES(?,?)',[(bid,r['id']) for r in rows])
    if not start_paused:launch(mode,bid)
    return bid


def launch(mode,bid):
    if mode=='live':
        from executor import launch as launch_processes
        launch_processes(mode,bid)
        return
    with MANAGER_LOCK:
        if (mode,bid) in ACTIVE:
            return
        ACTIVE.add((mode,bid))
    threading.Thread(target=drain,args=(mode,bid),daemon=True).start()


def drain(mode,bid):
    try:
        with db(mode) as c:
            concurrency = c.execute('SELECT concurrency FROM batches WHERE id=?',(bid,)).fetchone()[0]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = set()
            while True:
                with db(mode) as c:
                    batch = c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
                    state = batch['status']
                    if state == 'running':
                        from proxies import capacity
                        available=capacity(mode)
                        if not available:
                            c.execute("UPDATE batches SET status='paused' WHERE id=?",(bid,))
                            state='paused'
                    if state == 'running':
                        rows = c.execute("SELECT id FROM runs WHERE batch_id=? AND status='pending' ORDER BY id LIMIT ?",(bid,max(0,min(concurrency,available)-len(futures)))).fetchall()
                        for r in rows:
                            c.execute("UPDATE runs SET status='running',started_at=? WHERE id=?",(now(),r['id']))
                        # Commit claims before workers read them.
                        c.commit()
                        for r in rows:
                            futures.add(pool.submit(run_one,mode,r['id'],batch['profile']))
                    if not futures:
                        if state == 'running':
                            c.execute("UPDATE batches SET status='completed',finished_at=? WHERE id=?",(now(),bid))
                            break
                        if state == 'stopping':
                            c.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE batch_id=? AND status='pending'",(now(),bid))
                            c.execute("UPDATE batches SET status='stopped',finished_at=? WHERE id=?",(now(),bid))
                            break
                        if state in ('completed','stopped'):
                            break
                if futures:
                    done, futures = wait(futures,timeout=0.4,return_when=FIRST_COMPLETED)
                    for f in done:
                        f.result()
                else:
                    time.sleep(0.3)
    except Exception:
        with db(mode) as c:
            c.execute("UPDATE batches SET status='paused' WHERE id=?",(bid,))
            c.execute("UPDATE runs SET status='pending',error='队列异常，等待恢复' WHERE batch_id=? AND status='running'",(bid,))
    finally:
        with MANAGER_LOCK:
            ACTIVE.discard((mode,bid))


def run_one(mode,run_id,profile,proxy_override=None,lease_owner=None,only_steps=None):
    with db(mode) as c:
        run = c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        account = dict(c.execute('SELECT * FROM accounts WHERE id=?',(run['account_id'],)).fetchone())
        if profile is None:profile=c.execute('SELECT profile FROM batches WHERE id=?',(run['batch_id'],)).fetchone()[0]
        if only_steps is None:
            selection=c.execute('SELECT step_keys FROM batch_steps WHERE batch_id=?',(run['batch_id'],)).fetchone()
            if selection:only_steps=json.loads(selection[0])
    with account_lock(mode,account['id']):
        state, error, points, gained = 'completed','',account['points'],0
        before_points = account['points']
        verified_balance=False
        from protocol.task_policy import account_reason
        excluded=account_reason(mode,account,decrypt(account['credentials'])) if mode=='live' and profile in ('full','daily') else ''
        try:
            with db(mode) as c:
                unresolved = c.execute("SELECT 1 FROM redemptions WHERE account_id=? AND status IN ('unknown','pending')",(account['id'],)).fetchone()
            if unresolved:
                state,error = 'failed','存在待核实兑换订单，请核实后再执行任务'
            elif excluded:
                state,error,gained='skipped',excluded,None
            elif account['status']=='disabled':
                state,error = 'skipped','账号已停用，本批次已记录'
            elif profile!='app_check' and account['status']=='expired' and not decrypt(account['credentials']).get('_pool'):
                state,error = 'failed','登录态已过期，请更新凭证后重试'
            else:
                with db(mode) as c:
                    if mode=='live':
                        from protocol.runner import PLAN_VERSION
                        prior=c.execute("SELECT 1 FROM task_checkpoints WHERE account_id=? AND day=? AND step_key=? AND status='completed'",(account['id'],today(),f'plan:v{PLAN_VERSION}:{profile}')).fetchone()
                    else:prior = c.execute('SELECT 1 FROM ledger WHERE account_id=? AND day=? AND profile=?',(account['id'],today(),profile)).fetchone()
                if prior and only_steps is None and profile not in ('sync','login','app_check'):
                    state,error = 'skipped','今日该方案已完成，避免重复执行'
                elif mode=='demo':
                    labels = {'full':['同步账号信息','业务签到','任务中心','浏览任务与领奖','会员权益','活动积分领取','核对积分余额'], 'daily':['同步账号信息','每日签到','连签奖励','核对积分余额'], 'sync':['同步积分'], 'login':['小程序登录态获取 / 续期'], 'app_check':['官网 App 会话核验']}[profile]
                    for label in labels:
                        time.sleep(0.006)
                        with db(mode) as c:
                            c.execute('INSERT INTO steps(run_id,label,status,message,created_at) VALUES(?,?,?,?,?)',(run_id,label,'completed','演示步骤已完成',now()))
                    gained = (18+account['id']%53) if profile not in ('sync','login','app_check') else 0
                    points += gained
                    verified_balance=True
                else:
                    has_result = False
                    saw_step=False
                    raw=decrypt(account['credentials'])
                    saved=float(raw.get('saved_at') or 0)
                    # Old timestamps alone do not invalidate a token; verify with the platform first.
                    needs_login=profile!='app_check' and (account['status'] in ('expired','pending_login') or not raw.get('vivotoken') or profile=='login')
                    extra={'proxy':proxy_override,'lease_owner':lease_owner,'run_id':run_id} if proxy_override is not None else {'run_id':run_id}
                    if only_steps is not None:extra['only_steps']=only_steps
                    action='app_session_check' if profile=='app_check' else 'login' if profile=='login' else 'tasks'
                    for event in live_events(mode,account,action=action,profile=profile,refresh_login=needs_login,**extra):
                        if event['type']=='step_start':
                            saw_step=True
                            with db(mode) as c:
                                c.execute("INSERT INTO steps(run_id,label,status,message,created_at,step_key) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,step_key) WHERE step_key IS NOT NULL DO UPDATE SET status='running',message='执行中',created_at=excluded.created_at,elapsed_ms=0,request_count=0",(run_id,event['label'],'running','执行中',now(),event.get('step_key')))
                        elif event['type']=='step':
                            saw_step=True
                            with db(mode) as c:
                                if event.get('step_key'):
                                    c.execute('INSERT INTO steps(run_id,label,status,message,created_at,step_key,elapsed_ms,request_count) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,step_key) WHERE step_key IS NOT NULL DO UPDATE SET status=excluded.status,message=excluded.message,elapsed_ms=excluded.elapsed_ms,request_count=excluded.request_count',(run_id,event['label'],event['status'],event.get('message',''),now(),event['step_key'],event.get('elapsed_ms',0),event.get('request_count',0)))
                                else:c.execute('INSERT INTO steps(run_id,label,status,message,created_at) VALUES(?,?,?,?,?)',(run_id,event['label'],event['status'],event.get('message',''),now()))
                                if event.get('details') is not None:c.execute('UPDATE steps SET details=? WHERE run_id=? AND step_key=?',(json.dumps(event['details'],ensure_ascii=False),run_id,event.get('step_key')))
                            if event['status'] in ('failed','partial'):
                                state,error = 'partial','部分任务未完成，请查看执行明细'
                        elif event['type']=='result':
                            has_result = True
                            points = int(event['points'])
                            verified_balance=True
                            before_points = int(event.get('before',before_points))
                            gained = int(event.get('gained',0))
                        elif event['type']=='balance':
                            points=int(event['points'])
                            before_points=points
                            verified_balance=True
                            with db(mode) as c:
                                c.execute('UPDATE accounts SET points=?,points_synced_at=? WHERE id=?',(points,now(),account['id']))
                                c.execute('UPDATE runs SET before_points=? WHERE id=?',(points,run_id))
                        elif event['type']=='error':
                            from execution_policy import record_verification_hold
                            record_verification_hold(mode,account['id'],raw,event)
                            if not saw_step:
                                details=json.dumps({'provider_code':event.get('provider_code'),'diagnostics':event.get('diagnostics',{})},ensure_ascii=False)
                                with db(mode) as c:c.execute("INSERT INTO steps(run_id,label,status,message,created_at,step_key,details) VALUES(?,?,?,?,?,'session.check',?) ON CONFLICT(run_id,step_key) WHERE step_key IS NOT NULL DO UPDATE SET status=excluded.status,message=excluded.message,details=excluded.details",(run_id,'登录态与会话校验','failed',event['message'],now(),details))
                            state,error = ('pending' if event.get('retryable') and run['dispatch_attempts']<3 else 'failed'),event['message']
                            if profile=='app_check':
                                with db(mode) as c:c.execute('UPDATE app_sessions SET last_error=? WHERE account_id=?',(error,account['id']))
                        elif event['type']=='app_session':
                            with db(mode) as c:c.execute("UPDATE app_sessions SET status='verified',verified_at=?,last_error='' WHERE account_id=? AND version=?",(now(),account['id'],event.get('session_version')))
                        elif event['type']=='credentials':
                            raw=dict(event['credentials'])
                            with db(mode) as c:
                                c.execute("UPDATE accounts SET credentials=?,status='active' WHERE id=?",(encrypt(event['credentials']),account['id']))
                                if raw.get('openid') and not str(raw['openid']).startswith('wx:'):c.execute("INSERT OR IGNORE INTO account_aliases VALUES('vivo',?,?)",(raw['openid'],account['id']))
                    if not has_result and state!='pending':
                        state,error = 'failed',error or '未收到积分结果，已保留执行步骤'
                        gained=None
        except ValueError as exc:
            state,error = 'failed',str(exc)
        except Exception:
            state,error = 'failed','执行异常，请检查参考项目、账号凭证及网络后重试'
        with db(mode) as c:
            c.execute('UPDATE runs SET status=?,before_points=?,gained=?,error=?,finished_at=? WHERE id=?',(state,before_points,gained,error,now(),run_id))
            if profile in ('sync','login','app_check'):
                c.execute('UPDATE accounts SET points=? WHERE id=?',(max(0,points),account['id']))
            else:c.execute('UPDATE accounts SET points=?,gained=?,task_status=?,last_run=? WHERE id=?',(max(0,points),gained,state,now(),account['id']))
            if verified_balance:
                c.execute('UPDATE accounts SET points_synced_at=? WHERE id=?',(now(),account['id']))
            if state=='completed' and only_steps is None and profile not in ('sync','login','app_check'):
                c.execute('INSERT OR IGNORE INTO ledger VALUES(?,?,?,?)',(account['id'],today(),profile,gained))
                if mode=='live':
                    from protocol.runner import PLAN_VERSION
                    c.execute('INSERT OR REPLACE INTO task_checkpoints VALUES(?,?,?,?,?,?,?,?)',(account['id'],today(),f'plan:v{PLAN_VERSION}:{profile}','completed','当前任务版本已完成',0,0,now()))
            if gained:
                c.execute('INSERT INTO point_history(account_id,delta,reason,created_at) VALUES(?,?,?,?)',(account['id'],gained,'任务收益',now()))


def control(mode,bid,action,internal=False):
    with MANAGER_LOCK, db(mode) as c:
        batch = c.execute('SELECT * FROM batches WHERE id=?',(bid,)).fetchone()
        if not batch:
            raise ValueError('批次不存在')
        workflow=c.execute("SELECT * FROM workflows WHERE batch_id=? AND status='active'",(bid,)).fetchone()
        if workflow and not internal and action=='resume' and workflow['stage'] in ('waiting_proxy','discovering'):
            raise ValueError('全量流程正在准备代理与账号，准备完成后会自动执行')
        target = {'pause':('running','paused'),'resume':('paused','running'),'stop':(('running','paused'),'stopping')}.get(action)
        if not target or batch['status'] not in (target[0] if isinstance(target[0],tuple) else (target[0],)):
            raise ValueError('当前批次状态不支持此操作')
        if action=='resume':
            if c.execute("SELECT 1 FROM batches WHERE id!=? AND status IN ('running','stopping')",(bid,)).fetchone():raise ValueError('已有执行中的批次，请先暂停或结束后再继续')
            option=c.execute('SELECT scope FROM batch_options WHERE batch_id=?',(bid,)).fetchone()
            if option and option[0]=='all' or workflow and not option:
                c.execute('INSERT OR IGNORE INTO runs(batch_id,account_id) SELECT ?,id FROM accounts',(bid,))
                c.execute('UPDATE batches SET total=(SELECT count(*) FROM runs WHERE batch_id=?) WHERE id=?',(bid,bid))
        c.execute('UPDATE batches SET status=? WHERE id=?',(target[1],bid))
        if workflow and not internal:
            if action=='pause':
                c.execute("UPDATE workflows SET stage='user_paused',message='管理员已暂停',updated_at=? WHERE id=?",(now(),workflow['id']))
            elif action=='resume':
                c.execute("UPDATE workflows SET stage='running',message='继续执行',updated_at=? WHERE id=?",(now(),workflow['id']))
            elif action=='stop':
                c.execute("UPDATE workflows SET status='stopped',stage='user_stopped',message='管理员已停止',updated_at=? WHERE id=?",(now(),workflow['id']))
    if action in ('resume','stop'):
        launch(mode,bid)


def sync_pools(mode,proxy_id=None,source_ids=None):
    with MANAGER_LOCK, db(mode) as c:
        if c.execute("SELECT 1 FROM import_jobs WHERE status='running'").fetchone():
            raise ValueError('面板账号正在同步，请等待当前同步完成')
        if c.execute("SELECT 1 FROM batches WHERE status IN ('running','stopping')").fetchone():
            raise ValueError('请先结束当前任务批次，再同步面板账号')
        jid = 'IM'+uuid.uuid4().hex[:12].upper()
        c.execute('INSERT INTO import_jobs(id,status,created_at) VALUES(?,?,?)',(jid,'running',now()))
    def work():
        ended = False
        try:
            # Match cached vivo accounts to their original WeChat identities before upserting.
            with db(mode) as c:
                aliases, cached = {}, {}
                for row in c.execute('SELECT external_id,nickname,source,city,credentials FROM accounts'):
                    cred = decrypt(row['credentials'])
                    wx = cred.get('_wx_openid') or cred.get('wx_openid')
                    if wx:
                        aliases[wx] = row['external_id']
                        cached[wx] = {**cred,'openid':cred.get('openid') or row['external_id'],'_account_record_id':row['external_id'],'nickname':row['nickname'],'source':row['source'],'city':row['city']}
            extra={'panel_source_ids':source_ids}
            if proxy_id:
                with db(mode) as c:
                    chosen=c.execute("SELECT * FROM proxies WHERE id=? AND status='healthy'",(proxy_id,)).fetchone()
                if not chosen:raise ValueError('指定代理未通过验证')
                from proxies import proxy_url
                url=proxy_url(chosen)
                extra['proxy']={'http':url,'https':url}
            for event in live_events(mode,{'credentials':'','nickname':''},action='pools',**extra):
                with db(mode) as c:
                    if event['type']=='pool_start':
                        c.execute('UPDATE import_jobs SET total=? WHERE id=?',(event['total'],jid))
                    elif event['type']=='pool_accounts':
                        source_url=event.get('pool_url')
                        if source_url:
                            c.execute('INSERT INTO panel_sources(fingerprint,url,label,status,account_count,checked_at,last_error) VALUES(?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status,account_count=excluded.account_count,checked_at=excluded.checked_at,last_error=excluded.last_error',(hashlib.sha256(source_url.encode()).hexdigest(),encrypt({'url':source_url}),f"应用宝面板 {event['pool_index']:03}",'connected',len(event['items']),now(),''))
                            c.execute('UPDATE panel_sources SET online_count=?,offline_count=? WHERE fingerprint=?',(event.get('online',0),event.get('offline',0),hashlib.sha256(source_url.encode()).hexdigest()))
                            c.commit()
                        incoming = []
                        for item in event['items']:
                            wx = item['_wx_openid']
                            if wx in cached:
                                incoming.append({**item,**cached.pop(wx),'_pool':item['_pool'],'_wx_openid':wx,'panel_status':item.get('panel_status','unknown')})
                            elif wx not in aliases:
                                aliases[wx] = item['openid']
                                incoming.append(item)
                        added = 0
                        if incoming:
                            added = import_accounts(mode,incoming)['added']
                        c.execute('UPDATE import_jobs SET processed=processed+1,accounts=accounts+? WHERE id=?',(added,jid))
                    elif event['type']=='pool_error':
                        source_url=event.get('pool_url')
                        if source_url:
                            c.execute('INSERT INTO panel_sources(fingerprint,url,label,status,account_count,checked_at,last_error) VALUES(?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET status=excluded.status,checked_at=excluded.checked_at,last_error=excluded.last_error',(hashlib.sha256(source_url.encode()).hexdigest(),encrypt({'url':source_url}),f"应用宝面板 {event['pool_index']:03}",'unreachable',0,now(),'连接或读取失败'))
                        c.execute('UPDATE import_jobs SET processed=processed+1,failed=failed+1,message=? WHERE id=?',(f"面板 {event['pool_index']} 连接或数据读取失败",jid))
                    elif event['type']=='pool_end':
                        ended = True
            with db(mode) as c:
                row = c.execute('SELECT * FROM import_jobs WHERE id=?',(jid,)).fetchone()
                c.execute('UPDATE import_jobs SET status=?,message=? WHERE id=?',('completed' if ended and not row['failed'] else 'partial','已完成全量面板扫描' if ended else '同步中断，已导入账号保留；可重新同步',jid))
        except Exception:
            with db(mode) as c:
                c.execute("UPDATE import_jobs SET status='failed',message='同步失败，请检查参考项目与网络；已导入数据保留' WHERE id=?",(jid,))
    threading.Thread(target=work,daemon=True).start()
    return jid
