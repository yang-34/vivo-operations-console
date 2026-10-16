"""Proxy registry, auto-detection and shared egress leases. Credentials are plaintext by request."""
from __future__ import annotations
import hashlib
import ipaddress
import json
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor,as_completed
from contextlib import contextmanager
from datetime import datetime,timedelta
from urllib.parse import urlparse,quote,unquote
import requests
from store import db,encrypt,decrypt,now,TZ

SOURCES=[('monosans','https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/all.txt'),('proxifly/CN','https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/CN/data.txt')]
LOCK=threading.RLock()


def proxy_url(row):
    value=row['url'] if not isinstance(row,str) else row
    return value if '://' in value else decrypt(value)['url']


def parse_candidate(value,source='user'):
    value=str(value).strip();expires=None
    if not value:raise ValueError('空行')
    if '://' not in value and re.search(r'[|，,]',value):
        parts=[s.strip() for s in re.split(r'[|，,]',value)]
        if len(parts) not in (2,4,5):raise ValueError('需要 IP、端口、账号、密码、到期时间')
        host,port=parts[:2];user=parts[2] if len(parts)>2 else '';password=parts[3] if len(parts)>3 else ''
        if len(parts)==5 and parts[4]:
            try:
                date=datetime.fromisoformat(parts[4]);date=date+timedelta(days=1) if len(parts[4])==10 else date
                expires=(date.replace(tzinfo=TZ) if date.tzinfo is None else date.astimezone(TZ)).isoformat(timespec='seconds')
            except ValueError:raise ValueError('到期时间格式错误')
        auth=f'{quote(user,safe="")}:{quote(password,safe="")}@' if user else ''
        value=f'socks5h://{auth}{host}:{port}'
    if '://' not in value:value=('http://' if source.endswith('/http') else 'socks5h://')+value
    value=value.replace('socks5://','socks5h://')
    parsed=urlparse(value)
    try:port=parsed.port
    except ValueError:raise ValueError('端口错误')
    if parsed.scheme not in ('http','https','socks5h') or not parsed.hostname or not port:raise ValueError('代理地址或协议错误')
    try:
        address=ipaddress.ip_address(parsed.hostname)
        if address.is_multicast or address.is_unspecified or source!='user' and not address.is_global:raise ValueError('代理地址不可用')
    except ValueError as exc:
        if str(exc)=='代理地址不可用':raise
        if source!='user':raise ValueError('公共列表需要有效公网 IP')
    value=parsed.geturl()
    return {'url':value,'fingerprint':hashlib.sha256(value.encode()).hexdigest(),'expires_at':expires}


def import_lines(mode,lines,source='user'):
    added=0;ids=[];errors=[]
    with db(mode) as c:
        for index,line in enumerate(lines,1):
            if not str(line).strip():continue
            try:item=parse_candidate(line,source)
            except ValueError as exc:errors.append({'line':index,'error':str(exc)});continue
            old=c.execute('SELECT id FROM proxies WHERE fingerprint=?',(item['fingerprint'],)).fetchone()
            if old:
                pid=old[0]
                c.execute('UPDATE proxies SET url=?,expires_at=COALESCE(?,expires_at) WHERE id=?',(item['url'],item['expires_at'],pid))
            else:
                cursor=c.execute('INSERT INTO proxies(fingerprint,url,source,created_at,expires_at) VALUES(?,?,?,?,?)',(item['fingerprint'],item['url'],source,now(),item['expires_at']));pid=cursor.lastrowid;added+=1
            ids.append(pid)
    return {'added':added,'accepted':len(ids),'ids':list(dict.fromkeys(ids)),'errors':errors}


def add_candidates(mode,urls,source):return import_lines(mode,urls,source)['added']


def check_one(mode,row):
    if row.get('expires_at') and row['expires_at']<=now():
        with db(mode) as c:c.execute("UPDATE proxies SET status='expired',checked_at=?,last_error='代理已到期' WHERE id=?",(now(),row['id']))
        return False
    value=proxy_url(row);parsed=urlparse(value);entry=None
    try:
        started=time.monotonic()
        with socket.create_connection((parsed.hostname,parsed.port),timeout=4):entry=int((time.monotonic()-started)*1000)
    except OSError:
        with db(mode) as c:c.execute("UPDATE proxies SET status='failed',checked_at=?,last_error='入口 TCP 连接失败' WHERE id=?",(now(),row['id']))
        return False
    variants=[value]
    if row['source']=='user' and value.startswith('socks5h://'):variants.append(value.replace('socks5h://','http://',1))
    errors=[]
    for candidate in variants:
        session=requests.Session();session.trust_env=False;px={'http':candidate,'https':candidate};started=time.monotonic()
        try:
            response=session.get('https://pointh5.vivo.com.cn/api/mall/v5/home/categories.do',proxies=px,timeout=(5,8));response.raise_for_status();response.json()
            echo=session.get('https://myip.ipip.net',proxies=px,timeout=(5,8));echo.raise_for_status()
            match=re.search(r'\b(?:\d{1,3}\.){3}\d{1,3}\b',echo.text)
            if not match or not ipaddress.ip_address(match[0]).is_global:raise ValueError('出口查询无有效 IP')
            with db(mode) as c:c.execute("UPDATE proxies SET url=?,fingerprint=?,status='healthy',exit_ip=?,latency_ms=?,entry_latency_ms=?,checked_at=?,last_error='' WHERE id=?",(candidate,hashlib.sha256(candidate.encode()).hexdigest(),match[0],int((time.monotonic()-started)*1000),entry,now(),row['id']))
            return True
        except Exception as exc:errors.append(type(exc).__name__)
        finally:session.close()
    with db(mode) as c:c.execute("UPDATE proxies SET status='failed',entry_latency_ms=?,checked_at=?,last_error=? WHERE id=?",(entry,now(),'HTTPS 检测失败：'+','.join(errors),row['id']))
    return False


def refresh(mode,public=False,ids=None):
    with LOCK,db(mode) as c:
        if c.execute("SELECT 1 FROM proxy_jobs WHERE status='running'").fetchone():raise ValueError('已有代理检测任务')
        jid='PX'+uuid.uuid4().hex[:12].upper();c.execute('INSERT INTO proxy_jobs(id,status,created_at) VALUES(?,?,?)',(jid,'running',now()))
    def work():
        try:
            if public:
                session=requests.Session();session.trust_env=False
                for source,url in SOURCES:
                    try:r=session.get(url,timeout=15);r.raise_for_status();add_candidates(mode,r.text.splitlines()[:150],source)
                    except Exception:pass
                session.close()
            with db(mode) as c:
                used={r[0][5:] for r in c.execute("SELECT resource FROM execution_leases WHERE resource LIKE 'exit:%' AND expires_at>?",(time.time(),))}
                rows=[dict(r) for r in c.execute('SELECT * FROM proxies WHERE enabled=1') if (ids is None and (public or r['source']=='user') or ids is not None and r['id'] in ids) and r['exit_ip'] not in used]
                c.execute('UPDATE proxy_jobs SET total=? WHERE id=?',(len(rows),jid))
            with ThreadPoolExecutor(max_workers=8) as pool:
                for future in as_completed([pool.submit(check_one,mode,row) for row in rows]):
                    with db(mode) as c:c.execute('UPDATE proxy_jobs SET processed=processed+1,healthy=healthy+? WHERE id=?',(int(future.result()),jid))
            with db(mode) as c:c.execute("UPDATE proxy_jobs SET status='completed',message='检测完成，执行中的出口已跳过' WHERE id=?",(jid,))
        except Exception:
            with db(mode) as c:c.execute("UPDATE proxy_jobs SET status='failed',message='检测任务异常，已保留结果' WHERE id=?",(jid,))
    threading.Thread(target=work,daemon=True).start();return jid


def capacity(mode):
    with db(mode) as c:
        required=json.loads(c.execute("SELECT value FROM settings WHERE key='proxy_required'").fetchone()[0])
        if not required or mode=='demo':return 64
        return c.execute("SELECT count(DISTINCT exit_ip) FROM proxies WHERE status='healthy' AND enabled=1 AND (expires_at IS NULL OR expires_at>?)",(now(),)).fetchone()[0]


def select_bound_proxy(c,account_id,healthy,used,auto_switch=False):
    binding=c.execute('SELECT p.* FROM proxy_bindings b JOIN proxies p ON p.id=b.proxy_id WHERE b.account_id=?',(account_id,)).fetchone() if account_id else None
    good={row['id']:row for row in healthy}
    if binding and binding['id'] in good:
        row=good[binding['id']]
        return row if 'exit:'+row['exit_ip'] not in used else None
    if binding and not auto_switch:return None
    if binding:
        # Uncertain remote writes must be reconciled before changing egress.
        if c.execute("SELECT 1 FROM redemptions WHERE account_id=? AND status IN ('pending','unknown')",(account_id,)).fetchone():return None
        if c.execute("SELECT 1 FROM task_journals WHERE account_id=? AND state='pending' AND namespace!='sms'",(account_id,)).fetchone():return None
        if c.execute("SELECT 1 FROM proxy_switches WHERE account_id=? AND created_at>?",(account_id,(datetime.now(TZ)-timedelta(minutes=10)).isoformat(timespec='seconds'))).fetchone():return None
    selected=next((row for row in healthy if 'exit:'+row['exit_ip'] not in used),None)
    if selected and account_id:
        if binding:
            c.execute('INSERT INTO proxy_switches(account_id,previous_proxy,next_proxy,reason,created_at) VALUES(?,?,?,?,?)',(account_id,binding['id'],selected['id'],'原代理不可用、停用或到期，派发前切换',now()))
        c.execute('INSERT OR REPLACE INTO proxy_bindings VALUES(?,?,?)',(account_id,selected['id'],now()))
    return selected


def overview(mode,page=1,size=20,q='',status='all',source='all'):
    with db(mode) as c:
        counts={r['status']:r['n'] for r in c.execute('SELECT status,count(*) n FROM proxies GROUP BY status')}
        rows=[]
        for row in c.execute('SELECT p.*,count(b.account_id) bound_accounts FROM proxies p LEFT JOIN proxy_bindings b ON b.proxy_id=p.id GROUP BY p.id ORDER BY CASE WHEN p.source=\'user\' THEN 0 ELSE 1 END,p.id'):
            parsed=urlparse(proxy_url(row));item={k:row[k] for k in ('id','source','status','latency_ms','entry_latency_ms','checked_at','last_error','expires_at','enabled','bound_accounts')}
            item.update(url=proxy_url(row),endpoint=f'{parsed.hostname}:{parsed.port}',entry_ip=parsed.hostname,port=parsed.port,protocol=parsed.scheme,username=unquote(parsed.username or ''),password=unquote(parsed.password or ''),exit_ip=row['exit_ip'])
            if q and q.lower() not in (item['endpoint']+' '+item['username']+' '+item['exit_ip']).lower():continue
            if status!='all' and item['status']!=status:continue
            if source!='all' and item['source']!=source:continue
            rows.append(item)
        return {'items':rows[(page-1)*size:page*size],'total':len(rows),'counts':counts,'capacity':c.execute("SELECT count(DISTINCT exit_ip) FROM proxies WHERE status='healthy' AND enabled=1 AND (expires_at IS NULL OR expires_at>?)",(now(),)).fetchone()[0],
          'bound_accounts':c.execute('SELECT count(*) FROM proxy_bindings').fetchone()[0],'jobs':[dict(r) for r in c.execute('SELECT * FROM proxy_jobs ORDER BY created_at DESC,rowid DESC LIMIT 3')]}


@contextmanager
def lease(mode,account_id=None):
    owner='api-'+uuid.uuid4().hex;selected=None
    with LOCK,db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        for expired in c.execute('SELECT DISTINCT run_id FROM execution_leases WHERE expires_at<?',(time.time(),)).fetchall():
            c.execute("UPDATE runs SET status='pending',error='执行租约到期，等待恢复' WHERE id=? AND status='running'",(expired[0],))
        c.execute('DELETE FROM execution_leases WHERE expires_at<?',(time.time(),))
        required=json.loads(c.execute("SELECT value FROM settings WHERE key='proxy_required'").fetchone()[0])
        switching=c.execute("SELECT value FROM settings WHERE key='proxy_auto_switch'").fetchone()
        used={r[0] for r in c.execute('SELECT resource FROM execution_leases WHERE expires_at>?',(time.time(),))}
        if account_id and 'account:'+str(account_id) in used:raise ValueError('该账号正在执行，请稍后再试')
        if mode!='demo' and required:
            healthy=[dict(row) for row in c.execute("SELECT * FROM proxies WHERE status='healthy' AND enabled=1 AND (expires_at IS NULL OR expires_at>?) ORDER BY CASE WHEN source='user' THEN 0 ELSE 1 END,latency_ms,id",(now(),))]
            selected=select_bound_proxy(c,account_id,healthy,used,bool(switching and json.loads(switching[0])))
            if not selected:raise ValueError('没有空闲的可用代理，或需先核对未完成操作；不会回退直连')
            c.execute('INSERT INTO execution_leases VALUES(?,?,?,?)',('exit:'+selected['exit_ip'],owner,0,time.time()+120))
        elif mode=='live':
            if 'exit:direct' in used:raise ValueError('直连出口正在使用')
            c.execute('INSERT INTO execution_leases VALUES(?,?,?,?)',('exit:direct',owner,0,time.time()+120))
        if account_id:c.execute('INSERT INTO execution_leases VALUES(?,?,?,?)',('account:'+str(account_id),owner,0,time.time()+120))
    stop=threading.Event()
    def renew_lease():
        while not stop.wait(15):
            with db(mode) as c:c.execute('UPDATE execution_leases SET expires_at=? WHERE owner=?',(time.time()+120,owner))
    acquired=bool(selected or account_id or mode=='live')
    if acquired:threading.Thread(target=renew_lease,daemon=True).start()
    try:
        url=proxy_url(selected) if selected else None
        yield {'http':url,'https':url} if url else None
    finally:
        stop.set()
        if acquired:
            with db(mode) as c:c.execute('DELETE FROM execution_leases WHERE owner=?',(owner,))


def service_proxy(mode):return lease(mode,None)
