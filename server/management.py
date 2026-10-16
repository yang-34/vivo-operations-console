from __future__ import annotations
import hashlib
import json
import time
from urllib.parse import urlparse
from flask import Blueprint,jsonify,request
from store import db,decrypt,encrypt,now,account_public
import proxies

blueprint=Blueprint('management',__name__)


@blueprint.post('/api/accounts/maintenance')
def maintain_accounts():
    from engine import create_batch
    body=request.get_json() or {};profile=body.get('profile')
    if profile not in ('login','app_check'):raise ValueError('登录态维护只支持小程序续期或 App 会话核验')
    ids=body.get('account_ids')
    if not isinstance(ids,list) or not ids or len(ids)>10000:raise ValueError('请明确选择 1–10,000 个账号，不会默认处理全部账号')
    return jsonify(id=create_batch(mode(),profile,ids=ids,concurrency=max(1,min(8,int(body.get('concurrency',3)))),validation=True))


def mode():
    value=request.headers.get('X-Data-Mode','live')
    if value not in ('live','demo'):raise ValueError('无效环境')
    return value


@blueprint.get('/api/proxies')
def proxy_list():
    return jsonify(proxies.overview(mode(),max(1,int(request.args.get('page',1))),max(1,min(100,int(request.args.get('page_size',20)))),request.args.get('q',''),request.args.get('status','all'),request.args.get('source','all')))


@blueprint.post('/api/proxies/import')
def proxy_import():
    body=request.get_json() or {};lines=str(body.get('urls') or '').splitlines()
    if len(lines)>10000:raise ValueError('单次最多一万行')
    result=proxies.import_lines(mode(),lines,'user')
    if body.get('check',True) and result['ids']:
        try:result['job_id']=proxies.refresh(mode(),ids=result['ids'])
        except ValueError:result['check_message']='已导入，当前已有检测任务，可稍后检测所选代理'
    return jsonify(result)


@blueprint.get('/api/proxies/switches')
def proxy_switches():
    with db(mode()) as c:
        records=[dict(row) for row in c.execute('''SELECT s.*,a.nickname FROM proxy_switches s
          LEFT JOIN accounts a ON a.id=s.account_id ORDER BY s.id DESC LIMIT 100''')]
    return jsonify(items=records)


@blueprint.post('/api/proxies/refresh')
def proxy_refresh():
    body=request.get_json() or {}
    return jsonify(id=proxies.refresh(mode(),public=bool(body.get('public',False)),ids=body.get('ids')))


@blueprint.patch('/api/proxies/<int:pid>')
def proxy_update(pid):
    with db(mode()) as c:
        row=c.execute('SELECT * FROM proxies WHERE id=?',(pid,)).fetchone()
        if not row:return jsonify(error='代理不存在'),404
        if c.execute('SELECT 1 FROM execution_leases WHERE resource=? AND expires_at>?',('exit:'+row['exit_ip'],time.time())).fetchone():raise ValueError('该出口正在执行账号，请暂停并等待当前账号结束')
        c.execute('UPDATE proxies SET enabled=? WHERE id=?',(int(bool(request.get_json()['enabled'])),pid))
    return jsonify(ok=True)


@blueprint.get('/api/proxies/<int:pid>/accounts')
def proxy_accounts(pid):
    page=max(1,int(request.args.get('page',1)))
    with db(mode()) as c:
        total=c.execute('SELECT count(*) FROM proxy_bindings WHERE proxy_id=?',(pid,)).fetchone()[0]
        rows=[account_public(r) for r in c.execute('SELECT a.* FROM accounts a JOIN proxy_bindings b ON a.id=b.account_id WHERE b.proxy_id=? ORDER BY a.id LIMIT 20 OFFSET ?',(pid,(page-1)*20))]
    return jsonify(items=rows,total=total)


@blueprint.get('/api/panels')
def panel_list():
    page=max(1,int(request.args.get('page',1)));size=max(1,min(100,int(request.args.get('page_size',20))))
    query=request.args.get('q','');status=request.args.get('status','all')
    with db(mode()) as c:
        rows=[]
        for row in c.execute('SELECT * FROM panel_sources ORDER BY label'):
            item=dict(row);item['url']=decrypt(row['url'])['url'];item['has_api_key']=bool(item.pop('api_key',''))
            if query and query.lower() not in (item['label']+' '+item['url']).lower():continue
            if status=='online' and item['status']!='connected':continue
            if status=='offline' and item['status']!='unreachable':continue
            if status=='disabled' and item['enabled']:continue
            rows.append(item)
        counts={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM panel_sources GROUP BY status')}
    return jsonify(items=rows[(page-1)*size:page*size],total=len(rows),counts=counts)


@blueprint.post('/api/panels')
def add_panel():
    body=request.get_json() or {};url=str(body.get('url') or '').strip().rstrip('/')
    parsed=urlparse(url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.query or parsed.fragment:raise ValueError('请输入 HTTP/HTTPS 面板基础地址，不包含查询参数')
    fingerprint=hashlib.sha256(url.encode()).hexdigest();label=str(body.get('label') or parsed.netloc)[:100]
    with db(mode()) as c:
        if c.execute('SELECT 1 FROM panel_sources WHERE fingerprint=?',(fingerprint,)).fetchone():raise ValueError('该端点已存在')
        c.execute('INSERT INTO panel_sources(fingerprint,url,label,status,api_key) VALUES(?,?,?,?,?)',(fingerprint,encrypt({'url':url}),label,'unknown',encrypt({'key':str(body.get('api_key') or '')}) if body.get('api_key') else ''))
    return jsonify(fingerprint=fingerprint)


@blueprint.patch('/api/panels/<fingerprint>')
def edit_panel(fingerprint):
    body=request.get_json() or {}
    with db(mode()) as c:
        if not c.execute('SELECT 1 FROM panel_sources WHERE fingerprint=?',(fingerprint,)).fetchone():return jsonify(error='端点不存在'),404
        if 'enabled' in body:c.execute('UPDATE panel_sources SET enabled=? WHERE fingerprint=?',(int(bool(body['enabled'])),fingerprint))
        if 'label' in body:c.execute('UPDATE panel_sources SET label=? WHERE fingerprint=?',(str(body['label'])[:100],fingerprint))
        if 'api_key' in body:c.execute('UPDATE panel_sources SET api_key=? WHERE fingerprint=?',(encrypt({'key':str(body['api_key'])}),fingerprint))
    return jsonify(ok=True)


@blueprint.post('/api/panels/sync')
def sync_panels():
    from engine import sync_pools
    body=request.get_json() or {}
    return jsonify(id=sync_pools(mode(),source_ids=body.get('ids')))


@blueprint.get('/api/panels/<fingerprint>/accounts')
def panel_accounts(fingerprint):
    page=max(1,int(request.args.get('page',1)));state=request.args.get('status','all')
    with db(mode()) as c:
        where=' WHERE panel_fingerprint=?';values=[fingerprint]
        if state!='all':where+=' AND panel_status=?';values.append(state)
        total=c.execute('SELECT count(*) FROM accounts'+where,values).fetchone()[0]
        rows=[account_public(r) for r in c.execute('SELECT * FROM accounts'+where+' ORDER BY id LIMIT 20 OFFSET ?',[*values,(page-1)*20])]
    return jsonify(items=rows,total=total)


@blueprint.get('/api/engine')
def engine_stats():
    from executor import stats
    return jsonify(stats(mode()))
