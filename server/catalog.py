"""Durable, account-scoped live catalogue refresh jobs with resumable scopes."""
import json,threading,uuid
from pathlib import Path
from flask import Blueprint,jsonify,request
from store import db,now
from engine import live_events
from protocol.market import scope_key

blueprint=Blueprint('catalog',__name__)
ACTIVE=set();LOCK=threading.Lock()


def mode():
    value=request.headers.get('X-Data-Mode','demo')
    if value not in ('demo','live'):raise ValueError('无效数据模式')
    return value

@blueprint.get('/api/tasks/catalog')
def task_capabilities():
    from protocol.source_bridge import task_catalog
    tasks=task_catalog()
    from protocol.task_policy import policy
    if policy():return jsonify(items=[t for t in tasks if t['default_enabled']],removed=[t for t in tasks if not t['default_enabled']],spending_tasks_enabled=False)
    return jsonify(items=tasks,removed=[],spending_tasks_enabled=False)


def upsert(mode,job,scope,products):
    key=scope_key(scope)
    with db(mode) as c:
        for item in products:
            c.execute('''INSERT INTO products(id,name,category,price,stock,image,description,source,metadata,updated_at,scope_key,seen_job,remote_active)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET name=excluded.name,category=excluded.category,
              price=excluded.price,stock=excluded.stock,image=excluded.image,description=excluded.description,source=excluded.source,
              metadata=excluded.metadata,updated_at=excluded.updated_at,scope_key=excluded.scope_key,seen_job=excluded.seen_job,remote_active=1''',
              (item['id'],item['name'],item['category'],item['price'],item['stock'],item['image'],item['description'],'live',json.dumps(item['metadata'],ensure_ascii=False),now(),key,job))
        c.execute("UPDATE products SET stock=0,remote_active=0,updated_at=? WHERE scope_key=? AND seen_job!=? AND source='live'",(now(),key,job))
        c.execute("INSERT INTO catalog_scopes VALUES(?,?,'completed','') ON CONFLICT(job_id,scope_key) DO UPDATE SET status='completed',error=''",(job,key))
        c.execute('UPDATE catalog_jobs SET processed=processed+1,upserted=upserted+? WHERE id=?',(len(products),job))


def run(mode,jid):
    with LOCK:
        if jid in ACTIVE:return
        ACTIVE.add(jid)
    def work():
        try:
            with db(mode) as c:
                job=c.execute('SELECT * FROM catalog_jobs WHERE id=?',(jid,)).fetchone()
                if job['status']=='cancelled':return
                account=dict(c.execute('SELECT * FROM accounts WHERE id=?',(job['account_id'],)).fetchone())
                completed=[r[0] for r in c.execute("SELECT scope_key FROM catalog_scopes WHERE job_id=? AND status='completed'",(jid,))]
                c.execute("UPDATE catalog_jobs SET status='running',error='' WHERE id=?",(jid,))
            options=json.loads(job['options'])
            for event in live_events(mode,account,action='catalog',catalog_scope=job['scope'],catalog_cities=options.get('cities'),catalog_completed=completed):
                if event['type']=='catalog_plan':
                    with db(mode) as c:c.execute('UPDATE catalog_jobs SET total=? WHERE id=?',(event['total'],jid))
                elif event['type']=='catalog_scope':upsert(mode,jid,event['scope'],event['products'])
                elif event['type']=='catalog_error':
                    with db(mode) as c:
                        c.execute("INSERT OR REPLACE INTO catalog_scopes VALUES(?,?,'failed',?)",(jid,scope_key(event['scope']),event['message']))
                        c.execute('UPDATE catalog_jobs SET processed=processed+1,failed=failed+1,error=? WHERE id=?',(event['message'],jid))
                elif event['type']=='error':raise ValueError(event['message'])
                with db(mode) as c:
                    if c.execute('SELECT status FROM catalog_jobs WHERE id=?',(jid,)).fetchone()[0]=='cancelled':return
            with db(mode) as c:c.execute("UPDATE catalog_jobs SET status=CASE WHEN failed>0 THEN 'partial' ELSE 'completed' END,finished_at=? WHERE id=?",(now(),jid))
        except Exception as error:
            with db(mode) as c:c.execute("UPDATE catalog_jobs SET status='failed',error=?,finished_at=? WHERE id=? AND status!='cancelled'",(str(error)[:250],now(),jid))
        finally:
            with LOCK:ACTIVE.discard(jid)
    threading.Thread(target=work,daemon=True,name='catalog-refresh').start()


@blueprint.get('/api/catalog/jobs')
def jobs():
    with db(mode()) as c:rows=[dict(row) for row in c.execute('SELECT j.*,a.nickname FROM catalog_jobs j JOIN accounts a ON a.id=j.account_id ORDER BY j.created_at DESC LIMIT 12')]
    return jsonify(items=rows)


@blueprint.post('/api/catalog/refresh')
def refresh():
    if mode()!='live':raise ValueError('实时更新需要切换至真实账号')
    body=request.get_json() or {};scope=body.get('scope','all');aid=int(body['account_id'])
    if scope not in ('all','national','cities'):raise ValueError('无效商品更新范围')
    selected=body.get('cities')
    if scope=='cities':
        known=json.loads(Path(__file__).parent.joinpath('protocol','cities.json').read_text(encoding='utf-8'))
        if not isinstance(selected,list) or not selected or any(city not in known for city in selected):raise ValueError('请选择有效城市')
    with db('live') as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT 1 FROM catalog_jobs WHERE status IN ('queued','running')").fetchone():raise ValueError('已有商品更新任务在执行')
        account=c.execute('SELECT status FROM accounts WHERE id=?',(aid,)).fetchone()
        if not account or account['status']=='disabled':raise ValueError('请选择可用账号')
        jid='CAT'+uuid.uuid4().hex[:12].upper()
        c.execute("INSERT INTO catalog_jobs(id,account_id,scope,status,options,created_at) VALUES(?,?,?,'queued',?,?)",(jid,aid,scope,json.dumps({'cities':selected},ensure_ascii=False),now()))
    run('live',jid)
    return jsonify(id=jid)


@blueprint.post('/api/catalog/jobs/<jid>/resume')
def resume(jid):
    with db(mode()) as c:
        c.execute('BEGIN IMMEDIATE')
        job=c.execute('SELECT * FROM catalog_jobs WHERE id=?',(jid,)).fetchone()
        if not job or job['status'] not in ('partial','failed','paused'):raise ValueError('该更新任务无法继续')
        if c.execute("SELECT 1 FROM catalog_jobs WHERE status IN ('queued','running')").fetchone():raise ValueError('已有商品更新任务在执行')
        c.execute("UPDATE catalog_jobs SET status='queued',processed=(SELECT count(*) FROM catalog_scopes WHERE job_id=? AND status='completed'),failed=0,finished_at=NULL WHERE id=?",(jid,jid))
    run(mode(),jid)
    return jsonify(ok=True)


@blueprint.post('/api/catalog/jobs/<jid>/cancel')
def cancel(jid):
    with db(mode()) as c:
        changed=c.execute("UPDATE catalog_jobs SET status='cancelled',finished_at=? WHERE id=? AND status IN ('queued','running','paused')",(now(),jid)).rowcount
    if not changed:raise ValueError('该更新任务已结束')
    return jsonify(ok=True)
