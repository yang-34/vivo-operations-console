"""Transaction boundaries and exactly-once local reservations for redemption."""
import json,time,uuid
from urllib.parse import urlparse
from flask import Blueprint,jsonify,request,Response
from pathlib import Path
import secrets
from store import db,now,encrypt,decrypt
from engine import account_lock,live_events
from proxies import lease

blueprint=Blueprint('commerce',__name__)

def request_mode():
    value=request.headers.get('X-Data-Mode','demo')
    if value not in ('demo','live'):raise ValueError('无效数据模式')
    return value

@blueprint.get('/captcha/widget')
def captcha_widget():
    root=Path(__file__).resolve().parents[1]
    script_path=root/'dist'/'captcha-widget.js'
    if not script_path.exists():script_path=root/'public'/'captcha-widget.js'
    script=script_path.read_text(encoding='utf-8')
    nonce=secrets.token_urlsafe(24)
    response=Response(f'<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="vivo-captcha"></div><p>请完成 vivo 官方安全验证</p><script nonce="{nonce}">{script}</script></body></html>',mimetype='text/html')
    response.headers['Content-Security-Policy']=f"default-src 'none'; script-src 'nonce-{nonce}' https://*.vivo.com.cn https://*.dingxiang-inc.com 'unsafe-eval'; connect-src https://*.vivo.com.cn https://*.dingxiang-inc.com; img-src https: data:; style-src 'unsafe-inline'; frame-src https://*.vivo.com.cn https://*.dingxiang-inc.com; base-uri 'none'"
    return response


def rows(mode,aid,pid):
    with db(mode) as c:
        account=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
        product=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
    if not account or not product:raise ValueError('账号或商品不存在')
    if account['status']=='disabled':raise ValueError('账号已停用')
    return dict(account),dict(product)


def preview_with_proxy(mode,account,product,proxy):
    with db(mode) as c:
        if c.execute("SELECT 1 FROM redemptions WHERE account_id=? AND status IN ('pending','unknown')",(account['id'],)).fetchone():raise ValueError('存在待核实订单，请先核对结果再预检其他商品')
    result=None
    for event in live_events(mode,account,action='exchange_preview',product=json.loads(product['metadata']),proxy=proxy):
        if event['type']=='exchange_preview':result=event
        elif event['type']=='error':raise ValueError(event['message'])
    if not result:raise ValueError('平台未返回商品预检结果')
    if result['product']['stock']<=0:raise ValueError('实时库存不足')
    if result['points']<result['product']['price']:raise ValueError('实时积分不足')
    with db(mode) as c:
        c.execute('UPDATE accounts SET points=?,points_synced_at=? WHERE id=?',(result['points'],now(),account['id']))
        c.execute('UPDATE products SET price=?,stock=?,metadata=?,updated_at=? WHERE id=?',(result['product']['price'],result['product']['stock'],json.dumps(result['product']['metadata'],ensure_ascii=False),now(),product['id']))
    return result


@blueprint.post('/api/redemptions/preflight')
def preflight():
    body=request.get_json() or {};mode=request_mode()
    aid=int(body['account_id']);pid=str(body['product_id']);account,product=rows(mode,aid,pid)
    if mode=='demo':return jsonify(price=product['price'],points=account['points'],stock=product['stock'],sms_required=False)
    with account_lock(mode,aid),lease(mode,aid) as proxy:result=preview_with_proxy(mode,account,product,proxy)
    key=uuid.uuid4().hex
    with db(mode) as c:c.execute('INSERT INTO exchange_preflights VALUES(?,?,?,?,?,?,?)',(key,aid,pid,result['product']['price'],json.dumps(result['product']['metadata'],ensure_ascii=False),time.time()+180,now()))
    return jsonify(id=key,price=result['product']['price'],points=result['points'],stock=result['product']['stock'],sms_required=False,verification='on_demand',expires_in=180,redemption_kind=result['product']['metadata'].get('redemption_kind','standard'))


@blueprint.post('/api/redemptions/<oid>/refresh')
def refresh_delivery(oid):
    current=request_mode()
    if current!='live':raise ValueError('演示订单无需查询平台')
    with db(current) as c:
        order=c.execute('SELECT * FROM redemptions WHERE id=?',(oid,)).fetchone()
        if not order or order['status']!='success' or not order['remote_order']:raise ValueError('仅成功的平台订单可以刷新激活信息')
        account=c.execute('SELECT * FROM accounts WHERE id=?',(order['account_id'],)).fetchone()
        product=c.execute('SELECT metadata FROM products WHERE id=?',(order['product_id'],)).fetchone()
    result=None
    with account_lock(current,order['account_id']),lease(current,order['account_id']) as proxy:
        for event in live_events(current,dict(account),action='exchange_order',product=json.loads(product[0]),order_id=order['remote_order'],proxy=proxy):
            if event['type']=='exchange_order':result=event['delivery']
            elif event['type']=='error':raise ValueError(event['message'])
    if result is None:raise ValueError('平台未返回订单激活信息')
    try:previous=json.loads(order['delivery']) if str(order['delivery']).startswith('{') else {}
    except (ValueError,TypeError):previous={}
    delivery=json.dumps({**previous,**result},ensure_ascii=False)
    with db(current) as c:
        c.execute('UPDATE redemptions SET delivery=? WHERE id=?',(delivery,oid))
        return jsonify(dict(c.execute('SELECT o.*,a.nickname FROM redemptions o JOIN accounts a ON a.id=o.account_id WHERE o.id=?',(oid,)).fetchone()))


@blueprint.post('/api/redemptions/stores')
def redemption_stores():
    body=request.get_json() or {};aid=int(body['account_id']);current=request_mode()
    with db(current) as c:
        pre=c.execute('SELECT * FROM exchange_preflights WHERE id=? AND account_id=? AND expires_at>?',(body.get('preflight_id'),aid,time.time())).fetchone()
        account=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
    if not pre or not account:raise ValueError('请先完成商品预检')
    with account_lock(current,aid),lease(current,aid) as proxy:
        for event in live_events(current,dict(account),action='exchange_stores',product=json.loads(pre['metadata']),proxy=proxy):
            if event['type']=='exchange_stores':return jsonify(items=event['items'])
            if event['type']=='error':raise ValueError(event['message'])
    raise ValueError('平台未返回可用门店')


@blueprint.post('/api/redemptions/verification')
def verification():
    body=request.get_json() or {};aid=int(body['account_id']);action=body.get('action');mode=request_mode()
    if mode!='live' or action not in ('sms','captcha'):raise ValueError('无效验证请求')
    with db(mode) as c:
        checked=c.execute('SELECT * FROM exchange_preflights WHERE id=? AND account_id=? AND expires_at>?',(str(body.get('preflight_id','')),aid,time.time())).fetchone()
        account=c.execute('SELECT * FROM accounts WHERE id=?',(aid,)).fetchone()
    if not checked or not account:raise ValueError('请先完成商品预检')
    if action=='sms':
        with db(mode) as c:
            c.execute('BEGIN IMMEDIATE');last=c.execute("SELECT updated_at FROM task_journals WHERE account_id=? AND namespace='sms' ORDER BY updated_at DESC LIMIT 1",(aid,)).fetchone()
            if last:
                from datetime import datetime
                if time.time()-datetime.fromisoformat(last[0]).timestamp()<60:raise ValueError('请等待 60 秒后再发送验证码')
            c.execute("INSERT OR REPLACE INTO task_journals VALUES(?,'sms','last','pending',?,?)",(aid,encrypt({}),now()))
    result=None
    with account_lock(mode,aid),lease(mode,aid) as proxy:
        for event in live_events(mode,dict(account),action='exchange_'+action,product=json.loads(checked['metadata']),proxy=proxy):
            if event['type']=='verification':result=event
            elif event['type']=='error':raise ValueError(event['message'])
    if not result or not result['ok']:raise ValueError((result or {}).get('message') or '平台未完成验证请求')
    if action=='captcha':
        sdk=result.get('sdk') or {};url=sdk.get('sdkUrl','');url='https:'+url if url.startswith('//') else url;p=urlparse(url)
        if p.scheme!='https' or not p.hostname or not (p.hostname.endswith('.vivo.com.cn') or p.hostname.endswith('.dingxiang-inc.com')) or p.username or p.password or p.port not in (None,443):raise ValueError('平台验证控件地址不在允许范围')
        return jsonify(sdk={'url':url,'params':sdk.get('sdkParams') or {}},nonce=uuid.uuid4().hex)
    return jsonify(ok=True,message='验证码已发送至账号绑定手机号')


def redeem(mode,body):
    aid=int(body['account_id']);pid=str(body['product_id']);key=str(body.get('idempotency_key') or '')
    if not key or len(key)>100:raise ValueError('缺少有效请求标识')
    for field,limit in (('sms_code',12),('ticket',8192),('const_id',256),('store_code',150)):
        value=body.get(field) or ''
        if not isinstance(value,str) or len(value)>limit or any(ord(ch)<32 for ch in value):raise ValueError('验证参数格式错误')
    with account_lock(mode,aid):
        with db(mode) as c:
            existing=c.execute('SELECT * FROM redemptions WHERE idempotency_key=?',(key,)).fetchone()
            if existing:
                if existing['account_id']!=aid or existing['product_id']!=pid:raise ValueError('请求标识已被其他兑换使用')
                return dict(existing)
        account,product=rows(mode,aid,pid)
        with lease(mode,aid) as proxy:
            if mode=='live':
                current=preview_with_proxy(mode,account,product,proxy)
                expected=product['price'];proof=body.get('preflight_id')
                if proof:
                    with db(mode) as c:pre=c.execute('SELECT * FROM exchange_preflights WHERE id=? AND account_id=? AND product_id=? AND expires_at>?',(proof,aid,pid,time.time())).fetchone()
                    if not pre:raise ValueError('预检已过期，请重新核对商品')
                    expected=pre['price']
                if current['product']['price']!=expected:raise ValueError('实时价格已变化，请重新预检确认')
                account['points']=current['points'];product.update(price=expected,stock=current['product']['stock'],metadata=json.dumps(current['product']['metadata']))
            if account['status'] not in ('active','expired') or account['points']<product['price'] or product['stock']<=0:raise ValueError('账号状态、余额或库存不满足兑换条件')
            with db(mode) as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute("SELECT 1 FROM redemptions WHERE account_id=? AND status IN ('pending','unknown')",(aid,)).fetchone():raise ValueError('存在待核实订单，请先核对后再兑换')
                if c.execute("SELECT 1 FROM runs r JOIN batches b ON b.id=r.batch_id WHERE r.account_id=? AND r.status IN ('pending','running') AND b.status='running'",(aid,)).fetchone():raise ValueError('该账号有执行中的任务批次，请稍后兑换')
                oid='EX'+uuid.uuid4().hex[:14].upper()
                c.execute('INSERT INTO redemptions(id,idempotency_key,account_id,product_id,product_name,price,image,art,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,key,aid,pid,product['name'],product['price'],product['image'],product['art'],'pending',now()))
                c.execute('UPDATE accounts SET points=points-? WHERE id=?',(product['price'],aid));c.execute('UPDATE products SET stock=stock-1 WHERE id=?',(pid,))
            result={'status':'failed','message':'未收到提交结果，兑换未发起'};submitted=False
            if mode=='demo':result={'status':'success','message':'演示兑换成功','delivery':'演示券码 DEMO-'+uuid.uuid4().hex[:10].upper()}
            else:
                try:
                    for event in live_events(mode,account,action='exchange',proxy=proxy,product=json.loads(product['metadata']),expected_price=product['price'],sms_code=str(body.get('sms_code') or ''),ticket=str(body.get('ticket') or ''),const_id=str(body.get('const_id') or ''),store_code=str(body.get('store_code') or '')):
                        if event['type']=='exchange_submit':
                            submitted=True;result={'status':'unknown','message':'平台已接收提交，兑换结果待核实'}
                        elif event['type']=='exchange':result=event
                        elif event['type']=='error':result={'status':'unknown' if submitted else 'failed','message':event['message']}
                except Exception:result={'status':'unknown' if submitted else 'failed','message':'请求未完成；已提交的订单保留待核实状态'}
            status=result['status'];delivery=result.get('delivery','')
            if isinstance(delivery,dict):delivery=json.dumps(delivery,ensure_ascii=False)
            with db(mode) as c:
                if status in ('failed','sms_required','captcha_required'):
                    c.execute('UPDATE accounts SET points=points+? WHERE id=?',(product['price'],aid));c.execute('UPDATE products SET stock=stock+1 WHERE id=?',(pid,))
                elif status=='success':
                    if result.get('points_after') is not None:c.execute('UPDATE accounts SET points=?,points_synced_at=? WHERE id=?',(result['points_after'],now(),aid))
                    c.execute('INSERT INTO point_history(account_id,delta,reason,created_at) VALUES(?,?,?,?)',(aid,-product['price'],'商品兑换：'+product['name'],now()))
                c.execute('UPDATE redemptions SET status=?,remote_order=?,delivery=?,message=? WHERE id=?',(status,result.get('remote_order',''),delivery,result.get('message',''),oid))
                return dict(c.execute('SELECT * FROM redemptions WHERE id=?',(oid,)).fetchone())
