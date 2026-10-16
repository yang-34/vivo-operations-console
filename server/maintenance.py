"""Local, persisted account discovery and renewal scheduling; never spends points."""
import json
import threading
import time
from datetime import datetime

from engine import create_batch,sync_pools
from proxies import capacity
from store import db,decrypt
from execution_policy import latest_login_results,renewal_candidates


def start():
    def loop():
        last_renew=0;last_app_check=0
        while True:
            try:
                from workflow import tick
                from proxies import refresh
                workflow_active=tick()
                with db('live') as c:
                    settings={r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM settings')}
                    busy=c.execute("SELECT 1 FROM batches WHERE status IN ('running','stopping')").fetchone()
                    latest=c.execute('SELECT * FROM import_jobs ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
                    active_sync=latest and latest['status']=='running'
                    due=not latest or time.time()-datetime.fromisoformat(latest['created_at']).timestamp()>int(settings.get('panel_sync_minutes',60))*60
                    paused=c.execute("SELECT 1 FROM batches WHERE status='paused'").fetchone()
                    accounts=[dict(r) for r in c.execute("SELECT * FROM accounts WHERE status!='disabled'")]
                    login_results=latest_login_results(c)
                    app_sessions=[dict(r) for r in c.execute("SELECT s.account_id,s.verified_at FROM app_sessions s JOIN accounts a ON a.id=s.account_id WHERE a.status!='disabled' AND s.status='verified'")]
                    proxy_job=c.execute('SELECT * FROM proxy_jobs ORDER BY created_at DESC,rowid DESC LIMIT 1').fetchone()
                if settings.get('auto_proxy_refresh') and not busy and not capacity('live') and (not proxy_job or proxy_job['status']!='running' and time.time()-datetime.fromisoformat(proxy_job['created_at']).timestamp()>1800):
                    refresh('live',public=True)
                permitted=not settings.get('proxy_required') or settings.get('public_proxy_auth_approved')
                if permitted and settings.get('auto_panel_sync') and due and not busy and not active_sync and capacity('live') and not workflow_active:
                    sync_pools('live')
                if permitted and settings.get('auto_renew') and not busy and not paused and not active_sync and time.time()-last_renew>900 and capacity('live') and not workflow_active:
                    renew=renewal_candidates(accounts,login_results,time.time())
                    if renew:
                        create_batch('live','login',ids=renew,concurrency=min(4,capacity('live')))
                        busy=True
                    last_renew=time.time()
                if permitted and settings.get('auto_app_session_check') and not busy and not paused and not active_sync and time.time()-last_app_check>3600 and capacity('live') and not workflow_active:
                    due=[r['account_id'] for r in app_sessions if not r['verified_at'] or time.time()-datetime.fromisoformat(r['verified_at']).timestamp()>86400]
                    if due:create_batch('live','app_check',ids=due,concurrency=min(4,capacity('live')))
                    last_app_check=time.time()
            except Exception:
                pass
            time.sleep(30)
    threading.Thread(target=loop,name='vivo-account-maintenance',daemon=True).start()
