"""Synthetic I/O replay; no account data or external network requests."""
import json,os,sys,time,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))


def simulate(mode,run_id,duration=.12,fail=False):
    started=time.perf_counter()
    if fail:raise RuntimeError('synthetic failure')
    time.sleep(duration)
    return {'id':run_id,'pid':os.getpid(),'started':started,'finished':time.perf_counter()}


def measure(slots,directory,count=24):
    from async_pool import AsyncProcessPool
    from store import db
    pool=AsyncProcessPool(2,slots,str(directory),'benchmark_async:simulate')
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            with db('live') as c:n=c.execute('SELECT count(*) FROM worker_status WHERE pid IN (?,?)',tuple(p.pid for p in pool.processes)).fetchone()[0]
            if n==2:break
            time.sleep(.05)
        assert n==2,'Workers did not initialize'
        started=time.perf_counter()
        futures=[pool.submit(None,'replay',i,.12) for i in range(count)]
        values=[future.result(timeout=20) for future in futures]
        elapsed=time.perf_counter()-started
        events=sorted([(r['started'],1) for r in values]+[(r['finished'],-1) for r in values])
        active=maximum=0
        for _,delta in events:active+=delta;maximum=max(maximum,active)
        assert sorted(r['id'] for r in values)==list(range(count))
        return {'processes':2,'slots_per_process':slots,'jobs':count,'seconds':round(elapsed,3),'peak_concurrent_accounts':maximum}
    finally:pool.shutdown(wait=True)


if __name__=='__main__':
    import store
    store.DATA=Path(__file__).resolve().parents[1]/'artifacts'/'async-benchmark'/uuid.uuid4().hex
    store.DATA.mkdir(parents=True);store.initialize('live')
    before=measure(1,store.DATA);after=measure(4,store.DATA)
    report={'scope':'synthetic I/O replay, startup excluded','real_accounts':0,'network_requests':0,'before':before,'after':after,'speedup':round(before['seconds']/after['seconds'],2)}
    (store.DATA.parent/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))
