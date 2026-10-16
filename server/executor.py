"""Persistent process pool backed by transactional account/proxy leases."""
from __future__ import annotations
import atexit
import json
import os
import threading
import time
import uuid
from concurrent.futures import wait,FIRST_COMPLETED
from async_pool import AsyncProcessPool
from store import db,now

LOCK=threading.Lock()
COORDINATORS={}
POOLS={}


def process_run(mode,run_id,proxy_id,owner):
    from engine import run_one
    from proxies import proxy_url
    try:
        with db(mode) as c:
            row=c.execute('SELECT * FROM proxies WHERE id=?',(proxy_id,)).fetchone() if proxy_id else None
            if proxy_id and not row:raise ValueError('绑定代理不存在，停止该账号')
            url=proxy_url(row) if row else None
            setting=c.execute("SELECT value FROM settings WHERE key='request_gap'").fetchone()
            gap=float(json.loads(setting[0])) if setting else .6
        if row:
            wait_seconds=float(row['last_used_at'] or 0)+gap-time.time()
            if wait_seconds>0:time.sleep(wait_seconds)
        proxy={'http':url,'https':url} if url else {}
        run_one(mode,run_id,None,proxy_override=proxy,lease_owner=owner)
        return {'pid':os.getpid(),'run_id':run_id}
    finally:
        if proxy_id:
            with db(mode) as c:c.execute('UPDATE proxies SET last_used_at=? WHERE id=?',(time.time(),proxy_id))
        

def claim_many(mode,bid,limit,count):
    """Atomically claim a pending account and an exclusive egress, across processes."""
    if count<=0:return []
    timestamp=time.time()
    with db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM execution_leases WHERE expires_at<?',(timestamp,))
        count=min(count,max(0,limit-c.execute("SELECT count(*) FROM runs WHERE batch_id=? AND status='running'",(bid,)).fetchone()[0]))
        if count<=0:return []
        batch=c.execute('SELECT status FROM batches WHERE id=?',(bid,)).fetchone()
        if not batch or batch[0]!='running':return []
        settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
        required=settings.get('proxy_required',True)
        proxies=[dict(r) for r in c.execute("SELECT p.*,count(b.account_id) assigned FROM proxies p LEFT JOIN proxy_bindings b ON p.id=b.proxy_id WHERE p.status='healthy' AND p.enabled=1 AND (p.expires_at IS NULL OR p.expires_at>?) GROUP BY p.id ORDER BY CASE WHEN p.source='user' THEN 0 ELSE 1 END,assigned,p.latency_ms,p.id",(now(),))]
        used={r[0] for r in c.execute('SELECT resource FROM execution_leases WHERE expires_at>?',(timestamp,))}
        by_id={p['id']:p for p in proxies}
        jobs=[]
        for run in c.execute("SELECT r.id,r.account_id,b.proxy_id FROM runs r LEFT JOIN proxy_bindings b ON b.account_id=r.account_id WHERE r.batch_id=? AND r.status='pending' ORDER BY r.id",(bid,)).fetchall():
            resource='account:'+str(run['account_id'])
            if resource in used:continue
            if required:
                from proxies import select_bound_proxy
                chosen=select_bound_proxy(c,run['account_id'],proxies,used,settings.get('proxy_auto_switch',False))
                if not chosen or 'exit:'+chosen['exit_ip'] in used:continue
                exit_resource='exit:'+chosen['exit_ip'];proxy_id=chosen['id']
            else:
                exit_resource='exit:direct';proxy_id=None
                if exit_resource in used:break
            owner=uuid.uuid4().hex
            c.executemany('INSERT INTO execution_leases VALUES(?,?,?,?)',[(resource,owner,run['id'],timestamp+60),(exit_resource,owner,run['id'],timestamp+60)])
            c.execute("UPDATE runs SET status='running',started_at=?,error='',dispatch_attempts=dispatch_attempts+1 WHERE id=? AND status='pending'",(now(),run['id']))
            jobs.append((run['id'],proxy_id,owner));used.update((resource,exit_resource))
            if len(jobs)>=count:break
    return jobs


def claim(mode,bid,limit):
    jobs=claim_many(mode,bid,limit,1)
    return jobs[0] if jobs else None


def recover_expired(mode):
    with db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        expired=[r[0] for r in c.execute('SELECT DISTINCT run_id FROM execution_leases WHERE expires_at<?',(time.time(),))]
        for rid in expired:
            c.execute("UPDATE runs SET status='pending',error='worker 中断，按步骤断点恢复' WHERE id=? AND status='running'",(rid,))
            c.execute("UPDATE steps SET status='pending',message='worker 中断，等待从断点恢复' WHERE run_id=? AND status='running'",(rid,))
        c.execute('DELETE FROM execution_leases WHERE expires_at<?',(time.time(),))


def launch(mode,bid):
    with LOCK:
        if bid in COORDINATORS:return
        COORDINATORS[bid]=True
    threading.Thread(target=coordinate,args=(mode,bid),daemon=True,name='process-scheduler').start()


def coordinate(mode,bid):
    futures={}
    try:
        from store import DATA
        with db(mode) as c:
            batch=c.execute('SELECT concurrency FROM batches WHERE id=?',(bid,)).fetchone()
            settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
            slots=max(1,min(16,int(settings.get('async_slots',4))))
            process_limit=max(1,min(64,int(settings.get('worker_processes',20))))
            from proxies import capacity
            available=capacity(mode)
            if not available:
                c.execute("UPDATE batches SET status='paused' WHERE id=?",(bid,));return
            limit=max(1,min(process_limit*slots,int(batch[0]),available))
            pool_size=min(process_limit,(limit+slots-1)//slots)
            pool_key=(pool_size,slots)
        with LOCK:
            for key,old in list(POOLS.items()):
                if key!=pool_key and old.idle():old.shutdown(wait=True);POOLS.pop(key,None)
            pool=POOLS.get(pool_key)
            if pool is None:
                pool=AsyncProcessPool(pool_size,slots,str(DATA))
                POOLS[pool_key]=pool
        last_heartbeat=0.
        last_recovery=0.
        while True:
            if time.time()-last_recovery>10:
                recover_expired(mode);last_recovery=time.time()
            if time.time()-last_heartbeat>10:
                with db(mode) as c:
                    for _,_,owner in futures.values():c.execute('UPDATE execution_leases SET expires_at=? WHERE owner=?',(time.time()+60,owner))
                last_heartbeat=time.time()
            with db(mode) as c:state=c.execute('SELECT status FROM batches WHERE id=?',(bid,)).fetchone()[0]
            if state=='running':
                for job in claim_many(mode,bid,limit,max(0,limit-len(futures))):
                    rid,proxy_id,owner=job
                    futures[pool.submit(process_run,mode,rid,proxy_id,owner)]=job
            if futures:
                done,_=wait(futures,timeout=.5,return_when=FIRST_COMPLETED)
                for future in done:
                    rid,_,owner=futures.pop(future)
                    try:future.result()
                    except Exception as exc:
                        from concurrent.futures.process import BrokenProcessPool
                        if isinstance(exc,BrokenProcessPool):
                            with LOCK:POOLS.pop(pool_key,None)
                        with db(mode) as c:c.execute("UPDATE runs SET status=CASE WHEN dispatch_attempts>=3 THEN 'failed' ELSE 'pending' END,error=CASE WHEN dispatch_attempts>=3 THEN '执行进程连续异常三次，保留断点等待人工重试' ELSE 'worker 异常退出，保留断点等待恢复' END WHERE id=? AND status='running'",(rid,))
                    finally:
                        with db(mode) as c:c.execute('DELETE FROM execution_leases WHERE owner=?',(owner,))
            else:
                with db(mode) as c:
                    pending=c.execute("SELECT count(*) FROM runs WHERE batch_id=? AND status='pending'",(bid,)).fetchone()[0]
                    active=c.execute("SELECT count(*) FROM runs WHERE batch_id=? AND status='running'",(bid,)).fetchone()[0]
                    if state=='stopping':
                        c.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE batch_id=? AND status='pending'",(now(),bid))
                        c.execute("UPDATE batches SET status='stopped',finished_at=? WHERE id=?",(now(),bid));break
                    if state=='running' and not pending and not active:
                        c.execute("UPDATE batches SET status='completed',finished_at=? WHERE id=?",(now(),bid));break
                    if state=='running' and pending:
                        c.execute("UPDATE batches SET status='paused' WHERE id=?",(bid,));break
                    if state in ('paused','completed','stopped'):break
                time.sleep(.3)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Process coordinator interrupted')
        if 'pool' in locals() and pool.broken:
            with db(mode) as c:
                c.execute("UPDATE steps SET status='pending',message='执行进程中断，保留已有断点' WHERE status='running' AND run_id IN (SELECT id FROM runs WHERE batch_id=?)",(bid,))
                c.execute("UPDATE runs SET status='pending',error='执行进程中断，等待断点恢复' WHERE batch_id=? AND status='running'",(bid,))
                c.execute('DELETE FROM execution_leases WHERE run_id IN (SELECT id FROM runs WHERE batch_id=?)',(bid,))
        with db(mode) as c:c.execute("UPDATE batches SET status='paused' WHERE id=?",(bid,))
    finally:
        with LOCK:COORDINATORS.pop(bid,None)


def stats(mode='live'):
    with db(mode) as c:
        workers=[dict(r) for r in c.execute('SELECT * FROM worker_status WHERE heartbeat>? ORDER BY pid',(time.time()-20,))]
        setting=c.execute("SELECT value FROM settings WHERE key='worker_processes'").fetchone()
        slots=c.execute("SELECT value FROM settings WHERE key='async_slots'").fetchone()
        configured_slots=json.loads(slots[0]) if slots else 4
        active_slots=c.execute("SELECT count(*) FROM execution_leases l JOIN runs r ON r.id=l.run_id WHERE l.resource LIKE 'account:%' AND l.expires_at>? AND r.status='running'",(time.time(),)).fetchone()[0]
        return {'engine':'native-multiprocess','execution_model':'async-slots','async_slots':configured_slots,'busy_accounts':active_slots,'web_pid':os.getpid(),'external_script_dependency':False,'worker_processes':len(workers),'busy_processes':sum(r['state']=='busy' for r in workers),'workers':workers,
          'configured_processes':json.loads(setting[0]) if setting else 20,
          'request_count':c.execute('SELECT COALESCE(sum(request_count),0) FROM steps').fetchone()[0],
          'active_leases':c.execute('SELECT count(*) FROM execution_leases WHERE expires_at>?',(time.time(),)).fetchone()[0],
          'checkpoint_count':c.execute('SELECT count(*) FROM task_checkpoints').fetchone()[0]}


def close_pools():
    for pool in list(POOLS.values()):pool.shutdown(wait=False,cancel_futures=True)

atexit.register(close_pools)
