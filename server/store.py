from __future__ import annotations

import json
import os
import random
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('VIVO_DATA_DIR', str(ROOT / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
TZ = timezone(timedelta(hours=8))


def now():
    return datetime.now(TZ).isoformat(timespec='seconds')


def today():
    return datetime.now(TZ).date().isoformat()


@contextmanager
def db(mode='live'):
    conn = sqlite3.connect(DATA / f'{mode}.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=NORMAL')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def cipher():
    key_path = DATA / '.credential-key'
    if not key_path.exists():
        try:
            with key_path.open('xb') as f:
                f.write(Fernet.generate_key())
        except FileExistsError:
            pass
    return Fernet(key_path.read_bytes())


def encrypt(data):
    return cipher().encrypt(json.dumps(data, ensure_ascii=False).encode()).decode()


def decrypt(value):
    return json.loads(cipher().decrypt(value.encode())) if value else {}


def account_public(row):
    out = dict(row)
    out.pop('credentials', None)
    if row['credentials']:
        from protocol.devices import resolve_device
        device=resolve_device(decrypt(row['credentials']))
        out['device']={key:device.get(key) for key in ('brand','model','market_name','android_ver','sdk_int','screensize','density','profile_source','native_verified')}
    out['balance_state']='verified' if out.get('points_synced_at') else 'not_synced'
    if not out.get('points_synced_at'):
        out['points']=None
    phone = out.get('phone', '')
    if len(phone) >= 8:
        out['phone'] = phone[:3] + '****' + phone[-4:]
    return out


def initialize(mode):
    with db(mode) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS accounts(
          id INTEGER PRIMARY KEY, external_id TEXT NOT NULL UNIQUE, nickname TEXT NOT NULL,
          phone TEXT DEFAULT '', source TEXT DEFAULT '', city TEXT DEFAULT '全国',
          status TEXT NOT NULL DEFAULT 'active', points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0),
          gained INTEGER DEFAULT 0, task_status TEXT DEFAULT 'pending', last_run TEXT,
          credentials TEXT DEFAULT '', created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_account_points ON accounts(points DESC, id);
        CREATE INDEX IF NOT EXISTS idx_account_status ON accounts(status,task_status);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_steps(
          batch_id TEXT PRIMARY KEY,step_keys TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS products(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,price INTEGER NOT NULL CHECK(price>=0),
          stock INTEGER NOT NULL CHECK(stock>=0),image TEXT DEFAULT '',art TEXT DEFAULT 'gift',
          description TEXT DEFAULT '',source TEXT DEFAULT 'local',metadata TEXT DEFAULT '{}',updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,profile TEXT NOT NULL,status TEXT NOT NULL,
          total INTEGER NOT NULL,concurrency INTEGER NOT NULL,created_at TEXT NOT NULL,finished_at TEXT);
        CREATE TABLE IF NOT EXISTS runs(
          id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batches(id),account_id INTEGER NOT NULL REFERENCES accounts(id),
          status TEXT NOT NULL DEFAULT 'pending',before_points INTEGER,gained INTEGER DEFAULT 0,
          error TEXT DEFAULT '',started_at TEXT,finished_at TEXT,UNIQUE(batch_id,account_id));
        CREATE TABLE IF NOT EXISTS batch_options(
          batch_id TEXT PRIMARY KEY REFERENCES batches(id),scope TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id,status);
        CREATE INDEX IF NOT EXISTS idx_runs_account ON runs(account_id,status);
        CREATE TABLE IF NOT EXISTS steps(
          id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL REFERENCES runs(id),label TEXT NOT NULL,
          status TEXT NOT NULL,message TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_steps_run ON steps(run_id);
        CREATE TABLE IF NOT EXISTS ledger(
          account_id INTEGER NOT NULL REFERENCES accounts(id),day TEXT NOT NULL,profile TEXT NOT NULL,
          gained INTEGER NOT NULL,PRIMARY KEY(account_id,day,profile));
        CREATE TABLE IF NOT EXISTS point_history(
          id INTEGER PRIMARY KEY,account_id INTEGER REFERENCES accounts(id),delta INTEGER NOT NULL,
          reason TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_history_date ON point_history(created_at);
        CREATE TABLE IF NOT EXISTS redemptions(
          id TEXT PRIMARY KEY,idempotency_key TEXT NOT NULL UNIQUE,account_id INTEGER NOT NULL REFERENCES accounts(id),
          product_id TEXT NOT NULL REFERENCES products(id),product_name TEXT NOT NULL,price INTEGER NOT NULL,
          image TEXT DEFAULT '',art TEXT DEFAULT 'gift',status TEXT NOT NULL,remote_order TEXT DEFAULT '',
          delivery TEXT DEFAULT '',message TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS import_jobs(
          id TEXT PRIMARY KEY,status TEXT NOT NULL,total INTEGER DEFAULT 0,processed INTEGER DEFAULT 0,
          failed INTEGER DEFAULT 0,accounts INTEGER DEFAULT 0,message TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS legacy_history(
          external_id TEXT NOT NULL,day TEXT NOT NULL,label TEXT DEFAULT '',gained INTEGER DEFAULT 0,
          completed_at REAL DEFAULT 0,source TEXT NOT NULL,PRIMARY KEY(external_id,day,source));
        CREATE TABLE IF NOT EXISTS account_aliases(
          kind TEXT NOT NULL,value TEXT NOT NULL,account_id INTEGER NOT NULL REFERENCES accounts(id),
          PRIMARY KEY(kind,value));
        CREATE TABLE IF NOT EXISTS proxies(
          id INTEGER PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'unverified',exit_ip TEXT DEFAULT '',latency_ms INTEGER,
          checked_at TEXT,last_error TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proxy_bindings(
          account_id INTEGER PRIMARY KEY REFERENCES accounts(id),proxy_id INTEGER NOT NULL REFERENCES proxies(id),
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proxy_jobs(
          id TEXT PRIMARY KEY,status TEXT NOT NULL,total INTEGER DEFAULT 0,processed INTEGER DEFAULT 0,
          healthy INTEGER DEFAULT 0,message TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS panel_sources(
          fingerprint TEXT PRIMARY KEY,url TEXT NOT NULL,label TEXT NOT NULL,status TEXT DEFAULT 'unknown',
          account_count INTEGER DEFAULT 0,checked_at TEXT,last_error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS workflows(
          id TEXT PRIMARY KEY,status TEXT NOT NULL,batch_id TEXT,stage TEXT NOT NULL,
          message TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_leases(
          resource TEXT PRIMARY KEY,owner TEXT NOT NULL,run_id INTEGER NOT NULL,expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_status(
          pid INTEGER PRIMARY KEY,state TEXT NOT NULL,run_id INTEGER,heartbeat REAL NOT NULL,started_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS worker_slots(
          pid INTEGER NOT NULL,slot INTEGER NOT NULL,run_id INTEGER,heartbeat REAL NOT NULL,
          PRIMARY KEY(pid,slot));
        CREATE TABLE IF NOT EXISTS task_checkpoints(
          account_id INTEGER NOT NULL,day TEXT NOT NULL,step_key TEXT NOT NULL,status TEXT NOT NULL,
          message TEXT DEFAULT '',elapsed_ms INTEGER DEFAULT 0,request_count INTEGER DEFAULT 0,updated_at TEXT NOT NULL,
          PRIMARY KEY(account_id,day,step_key));
        CREATE TABLE IF NOT EXISTS protocol_cache(
          key TEXT PRIMARY KEY,payload TEXT NOT NULL,expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS catalog_jobs(
          id TEXT PRIMARY KEY,account_id INTEGER NOT NULL,scope TEXT NOT NULL,status TEXT NOT NULL,
          total INTEGER DEFAULT 0,processed INTEGER DEFAULT 0,upserted INTEGER DEFAULT 0,failed INTEGER DEFAULT 0,
          options TEXT NOT NULL DEFAULT '{}',error TEXT DEFAULT '',created_at TEXT NOT NULL,finished_at TEXT);
        CREATE TABLE IF NOT EXISTS catalog_scopes(
          job_id TEXT NOT NULL,scope_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',error TEXT DEFAULT '',
          PRIMARY KEY(job_id,scope_key));
        CREATE TABLE IF NOT EXISTS task_journals(
          account_id INTEGER NOT NULL,namespace TEXT NOT NULL,action_key TEXT NOT NULL,state TEXT NOT NULL,
          payload TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(account_id,namespace,action_key));
        CREATE TABLE IF NOT EXISTS account_scopes(account_id INTEGER PRIMARY KEY,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_sessions(
          account_id INTEGER PRIMARY KEY REFERENCES accounts(id),payload TEXT NOT NULL,version TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'unverified',verified_at TEXT,last_error TEXT DEFAULT '',updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS exchange_preflights(
          id TEXT PRIMARY KEY,account_id INTEGER NOT NULL,product_id TEXT NOT NULL,price INTEGER NOT NULL,
          metadata TEXT NOT NULL,expires_at REAL NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS proxy_switches(
          id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,previous_proxy INTEGER,next_proxy INTEGER,
          reason TEXT NOT NULL,created_at TEXT NOT NULL);
        ''')
        product_columns={r[1] for r in c.execute('PRAGMA table_info(products)')}
        for name,kind in [('scope_key',"TEXT DEFAULT ''"),('seen_job',"TEXT DEFAULT ''"),('remote_active','INTEGER DEFAULT 1')]:
            if name not in product_columns:c.execute(f'ALTER TABLE products ADD COLUMN {name} {kind}')
        step_columns={r[1] for r in c.execute('PRAGMA table_info(steps)')}
        if 'dispatch_attempts' not in {r[1] for r in c.execute('PRAGMA table_info(runs)')}:
            c.execute('ALTER TABLE runs ADD COLUMN dispatch_attempts INTEGER DEFAULT 0')
        for name,kind in [('step_key','TEXT'),('elapsed_ms','INTEGER DEFAULT 0'),('request_count','INTEGER DEFAULT 0'),('details',"TEXT DEFAULT '{}' ")]:
            if name not in step_columns:c.execute(f'ALTER TABLE steps ADD COLUMN {name} {kind}')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_step_key ON steps(run_id,step_key) WHERE step_key IS NOT NULL')
        if 'points_synced_at' not in [r[1] for r in c.execute('PRAGMA table_info(accounts)')]:
            c.execute('ALTER TABLE accounts ADD COLUMN points_synced_at TEXT')
        if 'expires_at' not in [r[1] for r in c.execute('PRAGMA table_info(proxies)')]:
            c.execute('ALTER TABLE proxies ADD COLUMN expires_at TEXT')
        proxy_columns={r[1] for r in c.execute('PRAGMA table_info(proxies)')}
        for name,kind in [('enabled','INTEGER DEFAULT 1'),('entry_latency_ms','INTEGER'),('last_used_at','REAL DEFAULT 0')]:
            if name not in proxy_columns:c.execute(f'ALTER TABLE proxies ADD COLUMN {name} {kind}')
        panel_columns={r[1] for r in c.execute('PRAGMA table_info(panel_sources)')}
        for name,kind in [('enabled','INTEGER DEFAULT 1'),('api_key',"TEXT DEFAULT ''"),('online_count','INTEGER DEFAULT 0'),('offline_count','INTEGER DEFAULT 0')]:
            if name not in panel_columns:c.execute(f'ALTER TABLE panel_sources ADD COLUMN {name} {kind}')
        account_columns={r[1] for r in c.execute('PRAGMA table_info(accounts)')}
        for name,kind in [('panel_status',"TEXT DEFAULT 'unknown'"),('panel_fingerprint',"TEXT DEFAULT ''")]:
            if name not in account_columns:c.execute(f'ALTER TABLE accounts ADD COLUMN {name} {kind}')
        c.execute('CREATE INDEX IF NOT EXISTS idx_bindings_proxy ON proxy_bindings(proxy_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_account_panel ON accounts(panel_fingerprint,panel_status)')
        # Proxy credentials are intentionally plaintext, as requested by the administrator.
        for row in c.execute('SELECT id,url FROM proxies').fetchall():
            if '://' not in row['url']:
                c.execute('UPDATE proxies SET url=? WHERE id=?',(decrypt(row['url'])['url'],row['id']))
        c.execute("INSERT OR IGNORE INTO settings VALUES('reference_path',?)", (json.dumps(''),))
        c.execute("INSERT OR IGNORE INTO settings VALUES('concurrency',?)",('1' if mode=='live' else '4',))
        for key,val in [('proxy_required',mode=='live'),('proxy_auto_switch',True),('spending_tasks_enabled',False),('auto_panel_sync',False),('panel_sync_minutes',60),('auto_renew',False),('auto_app_session_check',False),('auto_proxy_refresh',False),('public_proxy_auth_approved',False),('allow_http_panels',False),('worker_processes',20),('async_slots',4),('read_retry_count',2),('retry_backoff',.5),('request_gap',.6),('step_timeout_seconds',180)]:
            c.execute('INSERT OR IGNORE INTO settings VALUES(?,?)',(key,json.dumps(val)))
    if mode == 'demo':
        seed_demo()
        with db(mode) as c:
            c.execute('UPDATE accounts SET points_synced_at=COALESCE(points_synced_at,created_at)')
    else:
        migrate_device_profiles(mode)


def migrate_device_profiles(mode='live'):
    """An atomic, idempotent local migration; no remote calls or task dispatch."""
    from protocol.devices import ensure_device_profile
    changed=0
    with db(mode) as c:
        c.execute('BEGIN IMMEDIATE')
        for row in c.execute('SELECT id,external_id,credentials FROM accounts').fetchall():
            raw=decrypt(row['credentials'])
            assigned=ensure_device_profile(raw,row['external_id'],preserve_legacy=True)
            if assigned is not raw:
                c.execute('UPDATE accounts SET credentials=? WHERE id=?',(encrypt(assigned),row['id']))
                changed+=1
    return changed


def seed_demo():
    rng = random.Random(20261015)
    with db('demo') as c:
        if c.execute('SELECT count(*) FROM accounts').fetchone()[0]:
            return
        cities = ['深圳', '广州', '上海', '北京', '成都', '杭州', '武汉', '南京']
        names = ['林间有风', '小岛来信', '南山以南', '蓝色星球', '一杯冰美式', '阿泽', '晴天', '一路向北', '小陈同学', '城市漫游者', '晚风', 'MOMO']
        stamp = now()
        rows = []
        for i in range(1, 4001):
            gained = rng.randint(18, 78)
            state = 'expired' if i % 67 == 0 else ('disabled' if i % 191 == 0 else 'active')
            task = 'failed' if i % 53 == 0 else 'completed'
            rows.append((i, f'demo-{i:06}', names[(i-1) % len(names)] + (f'_{i}' if i > 12 else ''), f'13{rng.randint(100000000,999999999)}', f'账号池 {chr(65+(i%4))}', cities[i % 8], state, rng.randint(120, 16800), gained if task=='completed' else 0, task, stamp, stamp))
        c.executemany('INSERT INTO accounts(id,external_id,nickname,phone,source,city,status,points,gained,task_status,last_run,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        products = [
            ('demo-earbuds','vivo TWS 4 真无线耳机','数码配件',12800,16,'earbuds','轻盈入耳，静享好声音。兑换后按订单填写收货信息。'),
            ('demo-cup','vivo 轻量随行保温杯','生活好物',2800,128,'cup','400mL 便携保温杯，适合通勤与日常出行。'),
            ('demo-coffee','瑞幸咖啡 · 单杯兑换券','生活礼券',680,256,'coffee','指定饮品单杯兑换券。演示券码仅用于验证系统流程。'),
            ('demo-video','哔哩哔哩大会员月卡','影音会员',1500,86,'video','30 天大会员权益，领取后请在有效期内兑换。'),
            ('demo-bag','vivoLife 城市双肩包','生活好物',5600,42,'bag','轻量设计，大容量分区，陪伴每一次城市出发。'),
            ('demo-charge','vivo 44W 闪充充电器','数码配件',4800,35,'charger','轻巧随行的充电搭档，请确认设备兼容性。'),
            ('demo-travel','滴滴出行 · 10 元优惠券','生活礼券',520,320,'ticket','指定出行场景可用，具体使用规则以券面为准。'),
            ('demo-music','QQ 音乐绿钻月卡','影音会员',1200,0,'music','30 天音乐会员权益，库存补充后可兑换。'),
        ]
        c.executemany('INSERT INTO products(id,name,category,price,stock,art,description,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', [(*p,'demo',stamp) for p in products])
        c.execute("INSERT INTO batches VALUES('DEMO-DAILY','每日全量任务','full','completed',4000,8,?,?)",(stamp,stamp))
        c.executemany('INSERT INTO runs(batch_id,account_id,status,before_points,gained,started_at,finished_at,error) VALUES(?,?,?,?,?,?,?,?)', [('DEMO-DAILY',r[0],'failed' if r[9]=='failed' else 'completed',max(0,r[7]-r[8]),r[8],stamp,stamp,'演示：任务接口超时' if r[9]=='failed' else '') for r in rows])
        for d in range(7):
            day = datetime.now(TZ)-timedelta(days=6-d)
            value = sum(r[8] for r in rows) if d==6 else rng.randint(130000,194000)
            c.execute('INSERT INTO point_history(delta,reason,created_at) VALUES(?,?,?)',(value,'演示任务收益',day.replace(hour=9,minute=30).isoformat(timespec='seconds')))
        for idx, p in enumerate(products[:4]):
            acc = rows[idx]
            c.execute('INSERT INTO redemptions(id,idempotency_key,account_id,product_id,product_name,price,art,status,delivery,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(f'DEMO-ORDER-{idx+1:03}',f'seed-{idx}',acc[0],p[0],p[1],p[3],p[5],'success',('演示券码 VIVO-DEMO-0001' if idx>1 else '演示订单 · 待填写收货信息'),stamp))
