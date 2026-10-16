"""Persisted end-to-end run: wait for proxies, discover accounts, execute all, export."""
import csv
import json
import uuid
from pathlib import Path

from store import DATA,account_public,db,now
from engine import create_batch,control,sync_pools,launch
from proxies import capacity


def begin():
    with db('live') as c:
        previous=c.execute("SELECT * FROM workflows WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
        if previous:return dict(previous)
    bid=create_batch('live','full',concurrency=4,start_paused=True)
    wid='WF'+uuid.uuid4().hex[:12].upper()
    with db('live') as c:
        c.execute('INSERT INTO workflows VALUES(?,?,?,?,?,?,?)',(wid,'active',bid,'waiting_proxy','等待通过 HTTPS 验证的代理；不会回退直连',now(),now()))
        return dict(c.execute('SELECT * FROM workflows WHERE id=?',(wid,)).fetchone())


def export_report(wid,bid):
    folder=DATA/'reports';folder.mkdir(exist_ok=True)
    with db('live') as c:
        rows=[]
        for r in c.execute('SELECT a.*,r.status execution_status,r.error,r.gained batch_gained FROM accounts a JOIN runs r ON a.id=r.account_id WHERE r.batch_id=? ORDER BY (a.points_synced_at IS NOT NULL) DESC,a.points DESC,a.id',(bid,)):
            rows.append(account_public(r))
        summary={'workflow_id':wid,'batch_id':bid,'generated_at':now(),'accounts':len(rows),
          'balances_verified':sum(r['balance_state']=='verified' for r in rows),
          'points_total':sum(r['points'] or 0 for r in rows),
          'execution_statuses':{},'panel_statuses':{r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM panel_sources GROUP BY status')}}
    for r in rows:summary['execution_statuses'][r['execution_status']]=summary['execution_statuses'].get(r['execution_status'],0)+1
    fields=[('id','账号 ID'),('nickname','昵称'),('phone','手机号'),('points','可用积分'),('balance_state','积分校验状态'),('batch_gained','本批增量'),('execution_status','执行结果'),('error','结果说明'),('points_synced_at','积分同步时间')]
    with (folder/f'{wid}-积分排行.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow([v for k,v in fields])
        for row in rows:
            values=[]
            for key,_ in fields:
                value=str(row.get(key) if row.get(key) is not None else '')
                values.append("'"+value if value.lstrip().startswith(('=','+','-','@')) else value)
            writer.writerow(values)
    (folder/f'{wid}-汇总.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return summary


def tick():
    with db('live') as c:
        workflow=c.execute("SELECT * FROM workflows WHERE status='active' ORDER BY created_at LIMIT 1").fetchone()
        if not workflow:return False
        workflow=dict(workflow)
        batch=dict(c.execute('SELECT * FROM batches WHERE id=?',(workflow['batch_id'],)).fetchone())
        latest=c.execute('SELECT * FROM import_jobs ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
        settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
    stage=workflow['stage']
    if stage=='user_paused':return True
    if stage=='running' and batch['status'] in ('completed','stopped'):
        summary=export_report(workflow['id'],batch['id'])
        with db('live') as c:
            c.execute("UPDATE workflows SET status='completed',stage='completed',message=?,updated_at=? WHERE id=?",(f"已处理 {summary['accounts']} 个账号，已校验 {summary['balances_verified']} 个余额；汇总已保存",now(),workflow['id']))
        return True
    if settings.get('proxy_required') and not settings.get('public_proxy_auth_approved'):
        with db('live') as c:
            c.execute("UPDATE workflows SET stage='awaiting_authorization',message='公开代理已检测；等待真实账号 HTTPS 请求用途授权',updated_at=? WHERE id=?",(now(),workflow['id']))
        return True
    if not capacity('live'):
        return True
    if stage.startswith('waiting_batch:'):
        previous=stage.split(':',1)[1]
        with db('live') as c:
            predecessor=c.execute('SELECT status FROM batches WHERE id=?',(previous,)).fetchone()
        if not predecessor:return True
        if predecessor[0]=='stopped':
            with db('live') as c:c.execute("UPDATE workflows SET stage='user_paused',message='前序批次已停止，等待管理员继续',updated_at=? WHERE id=?",(now(),workflow['id']))
            return True
        if predecessor[0]!='completed':return True
        control('live',batch['id'],'resume',internal=True)
        with db('live') as c:c.execute("UPDATE workflows SET stage='running',message='前一轮复核已结束，继续剩余可恢复账号',updated_at=? WHERE id=?",(now(),workflow['id']))
        return True
    if stage in ('waiting_proxy','awaiting_authorization'):
        if latest and latest['status']=='running':
            next_stage='discovering'
        else:
            sync_pools('live');next_stage='discovering'
        with db('live') as c:
            c.execute('UPDATE workflows SET stage=?,message=?,updated_at=? WHERE id=?',(next_stage,'代理已就绪，正在从应用宝同步全部面板账号',now(),workflow['id']))
        return True
    if stage=='discovering':
        if latest and latest['status']=='running':return True
        with db('live') as c:
            c.execute('INSERT OR IGNORE INTO runs(batch_id,account_id) SELECT ?,id FROM accounts',(batch['id'],))
            count=c.execute('SELECT count(*) FROM runs WHERE batch_id=?',(batch['id'],)).fetchone()[0]
            c.execute('UPDATE batches SET total=? WHERE id=?',(count,batch['id']))
            c.execute("UPDATE workflows SET stage='running',message=?,updated_at=? WHERE id=?",('全量账号已纳入任务批次，正在逐个执行',now(),workflow['id']))
        if batch['status']=='paused':control('live',batch['id'],'resume',internal=True)
        return True
    if stage=='running':
        if batch['status']=='paused':
            control('live',batch['id'],'resume',internal=True)
    return True
