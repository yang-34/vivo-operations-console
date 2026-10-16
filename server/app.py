from __future__ import annotations

import csv
import io
import json
import os
import time
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from adapter import discover, import_accounts, import_inventory, reference_path
from engine import account_lock, control, create_batch, live_events, recover, sync_pools
from store import ROOT, TZ, account_public, db, initialize, now, today

app = Flask(__name__,static_folder=str(ROOT/'dist'),static_url_path='/')
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024
if os.environ.get('VIVO_TRUST_PROXY')=='1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1)
from authentication import install as install_authentication
install_authentication(app)


def mode():
    value = request.headers.get('X-Data-Mode','demo')
    if value not in ('demo','live'):
        raise ValueError('无效数据模式')
    return value


@app.before_request
def local_origin_guard():
    if request.method in ('POST','PATCH','DELETE','PUT'):
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc not in {request.host,'127.0.0.1:5173','localhost:5173','127.0.0.1:8100','localhost:8100'}:
            return jsonify(error='不允许跨站操作'),403


@app.errorhandler(Exception)
def error(exc):
    if isinstance(exc,HTTPException):
        return jsonify(error=exc.description),exc.code
    if isinstance(exc,(ValueError,TypeError,KeyError)):
        return jsonify(error=str(exc)),400
    app.logger.exception('API operation failed')
    return jsonify(error='服务处理失败，请检查本地服务日志'),500


def page_args():
    return max(1,int(request.args.get('page',1))),max(1,min(100,int(request.args.get('page_size',20))))


def batch_rows(c):
    return [dict(r) for r in c.execute('''SELECT b.*,
      SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) completed,
      SUM(CASE WHEN r.status IN ('failed','partial') THEN 1 ELSE 0 END) failed,
      SUM(CASE WHEN r.status='skipped' THEN 1 ELSE 0 END) skipped,
      SUM(CASE WHEN r.status='running' THEN 1 ELSE 0 END) running,
      SUM(CASE WHEN r.status='pending' THEN 1 ELSE 0 END) pending,
      SUM(CASE WHEN r.status='cancelled' THEN 1 ELSE 0 END) cancelled,
      COALESCE(SUM(r.gained),0) gained FROM batches b LEFT JOIN runs r ON r.batch_id=b.id
      GROUP BY b.id ORDER BY b.created_at DESC,b.rowid DESC''')]


@app.get('/api/health')
def health():
    return jsonify(status='ok',service='vivo-operations-console',time=now())


@app.get('/api/overview')
def overview():
    with db(mode()) as c:
        counts = dict(c.execute("SELECT count(*) total,SUM(status='active') active,SUM(status='expired') expired,SUM(status='disabled') disabled,SUM(status='pending_login') pending_login,SUM(points_synced_at IS NULL) unsynced,COALESCE(SUM(CASE WHEN points_synced_at IS NOT NULL THEN points ELSE 0 END),0) points FROM accounts").fetchone())
        batches = batch_rows(c)
        latest = batches[0] if batches else None
        earned = c.execute("SELECT COALESCE(SUM(delta),0) FROM point_history WHERE delta>0 AND substr(created_at,1,10)=?",(today(),)).fetchone()[0]
        orders = c.execute("SELECT count(*) FROM redemptions WHERE status='success'").fetchone()[0]
        top = [account_public(r) for r in c.execute('SELECT * FROM accounts ORDER BY (points_synced_at IS NULL),points DESC,id LIMIT 5')]
        recent = [dict(r) for r in c.execute('SELECT o.*,a.nickname FROM redemptions o JOIN accounts a ON a.id=o.account_id ORDER BY o.created_at DESC,o.rowid DESC LIMIT 4')]
        trend = []
        for i in range(6,-1,-1):
            day = (datetime.now(TZ)-timedelta(days=i)).date().isoformat()
            value = c.execute('SELECT COALESCE(SUM(CASE WHEN delta>0 THEN delta ELSE 0 END),0) earned,COALESCE(-SUM(CASE WHEN delta<0 THEN delta ELSE 0 END),0) spent FROM point_history WHERE substr(created_at,1,10)=?',(day,)).fetchone()
            trend.append({'date':day[5:].replace('-','/'),'earned':value['earned'],'spent':value['spent']})
        return jsonify(counts=counts,today_earned=earned,redemptions=orders,latest_batch=latest,trend=trend,top_accounts=top,recent_orders=recent,time=now())


def account_query():
    clauses,values = [],[]
    q = request.args.get('q','').strip()
    if q:
        clauses.append('(nickname LIKE ? OR phone LIKE ? OR external_id LIKE ?)')
        values.extend(['%'+q+'%']*3)
    for key in ('status','task_status','source'):
        if request.args.get(key) and request.args[key]!='all':
            clauses.append(f'{key}=?')
            values.append(request.args[key])
    if request.args.get('min_points'):
        clauses.append('points>=?')
        values.append(int(request.args['min_points']))
    return (' WHERE '+' AND '.join(clauses) if clauses else ''),values


@app.get('/api/accounts')
def accounts():
    page,size = page_args()
    where,values = account_query()
    sorts = {'points':'(points_synced_at IS NULL),points DESC,id','gained':'gained DESC,points DESC,id','id':'id'}
    sort = sorts.get(request.args.get('sort','id'),'id')
    with db(mode()) as c:
        total = c.execute('SELECT count(*) FROM accounts'+where,values).fetchone()[0]
        rows = c.execute('SELECT * FROM accounts'+where+' ORDER BY '+sort+' LIMIT ? OFFSET ?',[*values,size,(page-1)*size]).fetchall()
        sources = [r[0] for r in c.execute('SELECT DISTINCT source FROM accounts ORDER BY source')]
        counts = {r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM accounts GROUP BY status')}
    return jsonify(items=[account_public(r) for r in rows],total=total,page=page,page_size=size,sources=sources,counts=counts)


def csv_response(rows,fields):
    buf = io.StringIO(newline='')
    buf.write('\ufeff')
    writer = csv.writer(buf)
    writer.writerow([label for _,label in fields])
    for row in rows:
        values = []
        for key,_ in fields:
            val = str(row.get(key,'') if row.get(key) is not None else '')
            # Prevent a nickname from becoming an Excel formula.
            values.append("'"+val if val.lstrip().startswith(('=','+','-','@','\t','\r')) else val)
        writer.writerow(values)
    return app.response_class(buf.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=vivo-report.csv'})


@app.get('/api/accounts/export')
def export_accounts():
    where,values = account_query()
    with db(mode()) as c:
        rows = [account_public(r) for r in c.execute('SELECT * FROM accounts'+where+' ORDER BY (points_synced_at IS NULL),points DESC,id',values)]
    return csv_response(rows,[('id','账号 ID'),('nickname','账号昵称'),('phone','手机号'),('source','账号来源'),('points','可用积分'),('gained','最近收益'),('task_status','任务状态'),('last_run','最后执行时间')])


@app.post('/api/accounts/import')
def add_accounts():
    return jsonify(import_accounts(mode(),request.get_json()))


@app.post('/api/accounts/discover')
def discover_accounts():
    if mode()!='live':
        raise ValueError('请切换至真实账号模式后导入本地缓存')
    return jsonify(discover(mode()))


@app.post('/api/accounts/sync-pools')
def start_pool_sync():
    if mode()!='live':
        raise ValueError('请切换至真实账号模式后同步面板')
    return jsonify(id=sync_pools(mode()))


@app.get('/api/imports')
def imports():
    with db(mode()) as c:
        return jsonify(items=[dict(r) for r in c.execute('SELECT * FROM import_jobs ORDER BY created_at DESC,rowid DESC LIMIT 5')])


@app.patch('/api/accounts/<int:aid>')
def edit_account(aid):
    state = request.get_json().get('status')
    if state not in ('active','disabled'):
        raise ValueError('无效账号状态')
    with account_lock(mode(),aid), db(mode()) as c:
        account = c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        if not account:
            return jsonify(error='账号不存在'),404
        if account['status']=='expired' and state=='active':
            raise ValueError('登录态过期，请重新导入有效凭证')
        c.execute('UPDATE accounts SET status=? WHERE id=?',(state,aid))
    return jsonify(ok=True)


@app.get('/api/accounts/<int:aid>')
def account_detail(aid):
    with db(mode()) as c:
        row = c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        if not row:
            return jsonify(error='账号不存在'),404
        history = [dict(r) for r in c.execute('SELECT * FROM point_history WHERE account_id=? ORDER BY id DESC LIMIT 30',(aid,))]
        runs = [dict(r) for r in c.execute('SELECT * FROM runs WHERE account_id=? ORDER BY id DESC LIMIT 10',(aid,))]
    return jsonify(account=account_public(row),history=history,runs=runs)


@app.get('/api/batches')
def batches():
    with db(mode()) as c:
        return jsonify(items=batch_rows(c))


@app.post('/api/batches')
def start_batch():
    body = request.get_json() or {}
    return jsonify(id=create_batch(mode(),profile=body.get('profile','full'),ids=body.get('account_ids'),retry_batch=body.get('retry_batch'),concurrency=body.get('concurrency',4),only_steps=body.get('only_steps')))


@app.post('/api/batches/sample')
def sample_batch():
    import random
    body=request.get_json() or {};count=max(1,min(5,int(body.get('count',3))))
    with db(mode()) as c:
        candidates=[r['id'] for r in c.execute("SELECT id FROM accounts WHERE status IN ('active','pending_login') AND panel_status!='offline'")]
    if not candidates:raise ValueError('没有可抽样的账号')
    selected=random.SystemRandom().sample(candidates,min(count,len(candidates)))
    bid=create_batch(mode(),profile=body.get('profile','full'),ids=selected,concurrency=len(selected),validation=True)
    with db(mode()) as c:c.execute("UPDATE batches SET name=? WHERE id=?",(f'随机抽样验证 · {len(selected)} 个账号',bid))
    return jsonify(id=bid,account_ids=selected)


@app.post('/api/batches/<bid>/<action>')
def batch_control(bid,action):
    control(mode(),bid,action)
    return jsonify(ok=True)


@app.get('/api/batches/<bid>/runs')
def runs(bid):
    page,size = page_args()
    where,values = ' WHERE r.batch_id=?',[bid]
    if request.args.get('status') and request.args['status']!='all':
        where += ' AND r.status=?'
        values.append(request.args['status'])
    with db(mode()) as c:
        total = c.execute('SELECT count(*) FROM runs r'+where,values).fetchone()[0]
        rows = [dict(r) for r in c.execute('SELECT r.*,a.nickname,a.points,a.source FROM runs r JOIN accounts a ON a.id=r.account_id'+where+' ORDER BY r.id LIMIT ? OFFSET ?',[*values,size,(page-1)*size])]
    return jsonify(items=rows,total=total)


@app.get('/api/runs/<int:rid>/steps')
def steps(rid):
    with db(mode()) as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM steps WHERE run_id=? ORDER BY id',(rid,))]
        for row in rows:
            try:row['details']=json.loads(row.get('details') or '{}')
            except (ValueError,TypeError):row['details']={}
        return jsonify(items=rows)


@app.get('/api/batches/<bid>/export')
def export_batch(bid):
    with db(mode()) as c:
        rows = [dict(r) for r in c.execute('SELECT r.*,a.nickname,a.points FROM runs r JOIN accounts a ON a.id=r.account_id WHERE r.batch_id=? ORDER BY a.points DESC,a.id',(bid,))]
    return csv_response(rows,[('account_id','账号 ID'),('nickname','账号昵称'),('status','执行结果'),('points','可用积分'),('gained','积分增量'),('error','异常说明'),('finished_at','完成时间')])


@app.get('/api/products')
def products():
    page,size = page_args()
    terms,values = [],[]
    for key in ('category','source'):
        if request.args.get(key) and request.args[key]!='all':
            terms.append(key+'=?')
            values.append(request.args[key])
    if request.args.get('q'):
        terms.append('(name LIKE ? OR description LIKE ?)')
        values.extend(['%'+request.args['q']+'%']*2)
    if request.args.get('stock')=='1':
        terms.append('stock>0')
    where = ' WHERE '+' AND '.join(terms) if terms else ''
    order = {'price_asc':'price ASC','price_desc':'price DESC','stock':'stock DESC'}.get(request.args.get('sort'),'rowid ASC')
    with db(mode()) as c:
        total = c.execute('SELECT count(*) FROM products'+where,values).fetchone()[0]
        rows = [dict(r) for r in c.execute('SELECT * FROM products'+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',[*values,size,(page-1)*size])]
        categories = [r[0] for r in c.execute('SELECT DISTINCT category FROM products')]
    for r in rows:
        r.pop('metadata',None)
    return jsonify(items=rows,total=total,categories=categories)


@app.post('/api/products/import-reference')
def import_products():
    if mode()!='live':
        raise ValueError('请先切换至真实账号模式')
    return jsonify(import_inventory(mode()))


@app.get('/api/products/<path:pid>')
def product_detail(pid):
    with db(mode()) as c:row=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
    if not row:return jsonify(error='商品不存在'),404
    item=dict(row);item.pop('metadata',None)
    return jsonify(item)


@app.get('/api/redemptions')
def orders():
    page,size = page_args()
    terms,values = [],[]
    if request.args.get('q'):
        terms.append('(o.product_name LIKE ? OR a.nickname LIKE ? OR o.id LIKE ?)')
        values.extend(['%'+request.args['q']+'%']*3)
    if request.args.get('status') and request.args['status']!='all':
        terms.append('o.status=?')
        values.append(request.args['status'])
    where = ' WHERE '+' AND '.join(terms) if terms else ''
    base = ' FROM redemptions o JOIN accounts a ON a.id=o.account_id'
    with db(mode()) as c:
        total = c.execute('SELECT count(*)'+base+where,values).fetchone()[0]
        rows = [dict(r) for r in c.execute('SELECT o.*,a.nickname'+base+where+' ORDER BY o.created_at DESC,o.rowid DESC LIMIT ? OFFSET ?',[*values,size,(page-1)*size])]
        counts = dict(c.execute("SELECT count(*) total,COALESCE(SUM(status='success'),0) success,COALESCE(SUM(CASE WHEN status='success' THEN price ELSE 0 END),0) spent,COALESCE(SUM(status IN ('pending','unknown','sms_required','captcha_required')),0) pending FROM redemptions").fetchone())
    return jsonify(items=rows,total=total,counts=counts)


@app.post('/api/redemptions')
def redeem():
    from commerce import redeem as execute_redemption
    body=request.get_json()
    if not isinstance(body,dict):raise ValueError('兑换请求格式错误')
    return jsonify(execute_redemption(mode(),body))


@app.get('/api/settings')
def settings():
    with db(mode()) as c:
        settings = {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
    root = reference_path(mode())
    with db(mode()) as c:
        panels={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM panel_sources GROUP BY status')}
        history=c.execute('SELECT count(*) FROM legacy_history').fetchone()[0]
        counts={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM proxies GROUP BY status')}
        capacity=c.execute("SELECT count(DISTINCT exit_ip) FROM proxies WHERE status='healthy' AND enabled=1 AND (expires_at IS NULL OR expires_at>?)",(now(),)).fetchone()[0]
    return jsonify(**settings,adapter_available=True,reference_exists=root.is_dir(),mode=mode(),server_time=now(),proxy_summary={'counts':counts,'capacity':capacity,'total':sum(counts.values())},panels=panels,historical_records=history)




@app.get('/api/workflows')
def workflows():
    with db(mode()) as c:
        return jsonify(items=[dict(r) for r in c.execute('SELECT * FROM workflows ORDER BY created_at DESC LIMIT 5')])


@app.post('/api/workflows')
def start_workflow():
    if mode()!='live':raise ValueError('全量真实执行流程仅可在真实环境使用')
    from workflow import begin
    return jsonify(begin())








@app.post('/api/redemptions/<oid>/reconcile')
def reconcile_order(oid):
    body = request.get_json()
    target = body.get('status')
    note = str(body.get('note') or '').strip()
    if target not in ('success','failed') or len(note)<6:
        raise ValueError('请选择核实结果并填写至少 6 个字的核实说明')
    with db(mode()) as c:
        order = c.execute('SELECT * FROM redemptions WHERE id=?',(oid,)).fetchone()
    if not order:
        return jsonify(error='订单不存在'),404
    with account_lock(mode(),order['account_id']),db(mode()) as c:
        c.execute('BEGIN IMMEDIATE')
        order = c.execute('SELECT * FROM redemptions WHERE id=?',(oid,)).fetchone()
        if order['status']!='unknown':
            raise ValueError('仅待核实订单可登记核实结果，请刷新订单')
        if target=='failed':
            c.execute('UPDATE accounts SET points=points+? WHERE id=?',(order['price'],order['account_id']))
            c.execute('UPDATE products SET stock=stock+1 WHERE id=?',(order['product_id'],))
        else:
            c.execute('INSERT INTO point_history(account_id,delta,reason,created_at) VALUES(?,?,?,?)',(order['account_id'],-order['price'],'人工核实兑换：'+order['product_name'],now()))
        c.execute('UPDATE redemptions SET status=?,remote_order=?,message=? WHERE id=?',(target,str(body.get('remote_order') or order['remote_order'])[:150],'管理员核实：'+note[:500],oid))
    return jsonify(ok=True)


@app.put('/api/settings')
def save_settings():
    body = request.get_json()
    if body.get('spending_tasks_enabled'):raise ValueError('积分消耗操作必须逐项确认，不能通过全局开关自动启用')
    if not isinstance(body.get('reference_path',''),str):
        raise ValueError('缓存导入目录格式错误')
    with db(mode()) as c:previous={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
    concurrency = int(body.get('concurrency',previous.get('concurrency',20)))
    if not 1<=concurrency<=256:
        raise ValueError('并发数必须在 1–256 之间')
    with db(mode()) as c:
        for key,val in (('reference_path',body.get('reference_path',previous.get('reference_path',''))),('concurrency',concurrency)):
            c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(val,ensure_ascii=False)))
        for key in ('proxy_required','proxy_auto_switch','spending_tasks_enabled','auto_panel_sync','auto_renew','auto_app_session_check','auto_proxy_refresh','public_proxy_auth_approved','allow_http_panels'):
            if key in body:
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(bool(body[key]))))
        if 'panel_sync_minutes' in body:
            interval=max(15,min(1440,int(body['panel_sync_minutes'])))
            c.execute("INSERT OR REPLACE INTO settings VALUES('panel_sync_minutes',?)",(json.dumps(interval),))
        for key,lower,upper,cast in [('worker_processes',1,64,int),('async_slots',1,16,int),('read_retry_count',0,3,int),('retry_backoff',.1,5,float),('step_timeout_seconds',30,900,int),('request_gap',.2,10,float)]:
            if key in body:
                value=cast(body[key])
                if not lower<=value<=upper:raise ValueError('执行参数超出范围')
                c.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value)))
    return jsonify(ok=True)


@app.get('/')
def index():
    with db('live') as c:
        default=c.execute("SELECT value FROM settings WHERE key='default_mode'").fetchone()
    if default and json.loads(default[0])=='live':
        page=(ROOT/'dist'/'index.html').read_text(encoding='utf-8').replace('<html lang=', '<html data-mode="live" lang=')
        return app.response_class(page,mimetype='text/html')
    return send_from_directory(ROOT/'dist','index.html')


def setup():
    for current in ('demo','live'):
        initialize(current)
        recover(current)
        with db(current) as c:c.execute("UPDATE catalog_jobs SET status='paused',error='服务重启，等待继续' WHERE status IN ('queued','running')")
        with db(current) as c:
            c.execute("UPDATE redemptions SET status='unknown',message='服务重启，请先在原平台核实兑换结果' WHERE status='pending'")
            c.execute("UPDATE import_jobs SET status='partial',message='服务重启，同步已中断；已导入账号保留' WHERE status='running'")


from management import blueprint as management_blueprint
app.register_blueprint(management_blueprint)
from catalog import blueprint as catalog_blueprint
app.register_blueprint(catalog_blueprint)
from commerce import blueprint as commerce_blueprint
app.register_blueprint(commerce_blueprint)
from app_sessions import blueprint as app_sessions_blueprint
app.register_blueprint(app_sessions_blueprint)

if __name__=='__main__':
    setup()
    from maintenance import start as start_maintenance
    start_maintenance()
    port = int(os.environ.get('VIVO_WEB_PORT','8100'))
    print(f'vivo management: http://127.0.0.1:{port}',flush=True)
    try:
        from waitress import serve
        serve(app,host=os.environ.get('VIVO_BIND_HOST','127.0.0.1'),port=port,threads=16)
    except ImportError:
        app.run(host=os.environ.get('VIVO_BIND_HOST','127.0.0.1'),port=port,threaded=True,debug=False)
