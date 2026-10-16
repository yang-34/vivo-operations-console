"""Read-only discovery of the user's existing reference checkout. No remote calls on import."""
from __future__ import annotations
import json
from pathlib import Path

from store import db, encrypt, decrypt, now, DATA


def reference_path(mode):
    with db(mode) as c:
        return Path(json.loads(c.execute("SELECT value FROM settings WHERE key='reference_path'").fetchone()[0]))


def normalize_accounts(payload):
    if isinstance(payload,dict) and 'fields' in payload:
        payload = [payload]
    if isinstance(payload, dict):
        payload = payload.get('accounts', payload.get('data', payload))
    if isinstance(payload, dict):
        payload = [dict(v, openid=v.get('openid') or k) for k, v in payload.items() if isinstance(v, dict)]
    if not isinstance(payload, list):
        raise ValueError('需要 JSON 数组或包含 accounts 的账号缓存对象')
    if len(payload) > 100000:
        raise ValueError('单次最多导入 100,000 个账号，请拆分文件')
    result = []
    for i, r in enumerate(payload):
        if not isinstance(r, dict):
            raise ValueError(f'第 {i+1} 条账号格式错误')
        r = {**r, **(r.get('fields') or {})}
        external_id = str(r.get('_account_record_id') or r.get('openid') or r.get('external_id') or '').strip()
        if not external_id:
            raise ValueError(f'第 {i+1} 条缺少 openid；已取消本次导入')
        result.append((external_id, str(r.get('nickname') or r.get('_wx_nick') or r.get('wx_nick') or f'vivo_{external_id[-6:]}')[:120],str(r.get('phone') or r.get('phonenum') or ''),str(r.get('source') or '本地导入')[:200],str(r.get('city') or '全国')[:50], 'active' if r.get('vivotoken') else ('pending_login' if r.get('_pool') else 'expired'),encrypt(r)))
    return result


def import_accounts(mode, payload):
    from protocol.devices import ensure_device_profile
    rows = normalize_accounts(payload)
    with db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        before = c.execute('SELECT count(*) FROM accounts').fetchone()[0]
        for external,nick,phone,source,city,state,credential in rows:
            raw=decrypt(credential)
            wx=str(raw.get('_wx_openid') or raw.get('wx_openid') or '')
            existing=c.execute('SELECT * FROM accounts WHERE external_id=?',(external,)).fetchone()
            if not existing and wx:
                existing=c.execute("SELECT a.* FROM accounts a JOIN account_aliases x ON a.id=x.account_id WHERE x.kind='wx' AND x.value=?",(wx,)).fetchone()
            if existing:
                previous=decrypt(existing['credentials'])
                new_time=float(raw.get('saved_at') or raw.get('_saved_at') or 0)
                old_time=float(previous.get('saved_at') or previous.get('_saved_at') or 0)
                merged={**previous,**{k:v for k,v in raw.items() if v not in (None,'')}}
                if previous.get('protocol_device'):
                    merged['protocol_device']=previous['protocol_device']
                if old_time>new_time and previous.get('vivotoken'):
                    for key in ('vivotoken','openid','saved_at','imei','oaid'):
                        if previous.get(key): merged[key]=previous[key]
                if str(raw.get('openid','')).startswith('wx:') and previous.get('openid') and not str(previous['openid']).startswith('wx:'):
                    for key in ('openid','vivotoken','saved_at'):
                        if previous.get(key):merged[key]=previous[key]
                if not merged.get('vivotoken'):
                    state='pending_login' if merged.get('_pool') else 'expired'
                else:
                    state='active'
                raw=merged
                external=existing['external_id']
                if nick.startswith('vivo_') or not raw.get('nickname') and not raw.get('_wx_nick') and not raw.get('wx_nick'):
                    nick=existing['nickname']
                if not phone: phone=existing['phone']
            credential=encrypt(ensure_device_profile(raw,external,preserve_legacy=bool(existing)))
            c.execute('''INSERT INTO accounts(external_id,nickname,phone,source,city,status,credentials,created_at)
          VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(external_id) DO UPDATE SET nickname=excluded.nickname,
          phone=excluded.phone,source=excluded.source,city=excluded.city,
          status=CASE WHEN accounts.status='disabled' THEN accounts.status ELSE excluded.status END,
          credentials=excluded.credentials''',(external,nick,phone,source,city,state,credential,now()))
            aid=c.execute('SELECT id FROM accounts WHERE external_id=?',(external,)).fetchone()[0]
            c.execute("INSERT OR IGNORE INTO account_aliases VALUES('vivo',?,?)",(external,aid))
            if wx:
                c.execute("INSERT OR IGNORE INTO account_aliases VALUES('wx',?,?)",(wx,aid))
            if raw.get('_pool'):
                import hashlib
                c.execute('UPDATE accounts SET panel_fingerprint=?,panel_status=? WHERE id=?',(hashlib.sha256(raw['_pool'].rstrip('/').encode()).hexdigest(),raw.get('panel_status','unknown'),aid))
        added = c.execute('SELECT count(*) FROM accounts').fetchone()[0] - before
    return {'processed':len(rows),'added':added,'updated':len(rows)-added}


def import_legacy_ledger(mode,payload,source='task_ledger.json'):
    from protocol.devices import ensure_device_profile
    records=payload.get('done') if isinstance(payload,dict) else None
    day=payload.get('date') if isinstance(payload,dict) else None
    from datetime import date
    if not isinstance(records,dict):
        raise ValueError('历史任务台账需要 date 和 done 字段')
    date.fromisoformat(day)
    added=0
    with db(mode) as c:
        for external,record in records.items():
            if not isinstance(record,dict): continue
            label=str(record.get('label') or '历史账号')[:200]
            cur=c.execute('INSERT OR IGNORE INTO legacy_history VALUES(?,?,?,?,?,?)',(external,day,label,int(record.get('gained') or 0),float(record.get('ts') or 0),source))
            added+=cur.rowcount
            if not c.execute('SELECT 1 FROM accounts WHERE external_id=?',(external,)).fetchone():
                credential=encrypt(ensure_device_profile({},external))
                c.execute('INSERT INTO accounts(external_id,nickname,source,status,credentials,created_at) VALUES(?,?,?,?,?,?)',(external,label,source,'expired',credential,now()))
    return {'historical_records':len(records),'added_history':added,'day':day}


def discover(mode):
    root = reference_path(mode)
    candidates = [root/'青龙面板版'/'_cache'/'accounts.json',root/'青龙单文件版'/'_cache'/'accounts.json',root/'_cache'/'accounts.json',root/'_cache'/'session.json',root/'vivo官网App'/'04_脚本'/'_cache'/'accounts.json']
    accounts, sources = [], []
    for file in candidates:
        if not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding='utf-8-sig'))
            normalize_accounts(data)
            if isinstance(data,dict):
                data = data.get('accounts',data)
            if isinstance(data,dict) and 'fields' not in data:
                data = [dict(v,openid=v.get('openid') or k) for k,v in data.items() if isinstance(v,dict)]
            if isinstance(data,dict):
                data = [data]
            accounts.extend(data)
            sources.append(str(file.relative_to(root)))
        except (ValueError, TypeError):
            continue
    result = import_accounts(mode,accounts)
    return {**result,'sources':sources}


def import_inventory(mode):
    file = DATA/'catalog-source.json'
    if not file.is_file():file=reference_path(mode)/'web'/'data'/'inventory.json'
    if not file.is_file():
        raise ValueError('缺少商品快照，请将 inventory.json 上传至 data/catalog-source.json')
    data = json.loads(file.read_text(encoding='utf-8-sig'))
    rows = []
    groups = [('全国',data.get('national',{}))] + list((data.get('inventory') or {}).items())
    seen = set()
    for city, group in groups:
        for item in group.get('items',[]):
            if item.get('playMethod') != 0 or item.get('price') is None:
                continue
            key = f"{city}:{item.get('itemId')}"
            if key in seen:
                continue
            seen.add(key)
            metadata = {'itemId':item['itemId'],'categoryId':item.get('categoryId',47),'city':city,'vkCityId':group.get('vkCityId'),'channel':'point','source':0,'playMethod':item.get('playMethod')}
            rows.append((key,item.get('name') or '未命名商品','生活礼券' if item.get('itemType')==1 else '生活好物',max(0,int(item['price'])),max(0,int(item.get('stock') or 0)),item.get('image') or '',f'{city} · 本地库存快照，兑换时以平台实时结果为准','reference',json.dumps(metadata,ensure_ascii=False),now()))
    with db(mode) as c:
        c.executemany('''INSERT INTO products(id,name,category,price,stock,image,description,source,metadata,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,price=excluded.price,
        stock=excluded.stock,image=excluded.image,metadata=excluded.metadata,updated_at=excluded.updated_at''',rows)
    return {'imported':len(rows),'snapshot_at':data.get('updated_at')}
