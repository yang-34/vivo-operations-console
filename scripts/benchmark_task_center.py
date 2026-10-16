"""Protocol replay comparison. No live accounts and no network requests."""
import json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server'))
from protocol.tasks import execute_center

class Replay:
    def __init__(self):self.finished=set();self.calls=0;self.list_calls=0
    def sign_biz_names(self):return {}
    def h5_call(self,host,path,**kwargs):
        self.calls+=1;time.sleep(.002)
        tasks=[{'id':i,'name':f'Task {i}','taskComplete':i in self.finished,'completedTimes':int(i in self.finished)} for i in range(10)]
        if path.endswith('/listByType.do'):
            self.list_calls+=1;return {'retcode':0,'data':tasks}
        if path.endswith('/list.do'):
            self.list_calls+=1;return {'retcode':0,'data':{'tasks':[{'hasMore':False,'list':tasks}]}}
        return {'retcode':0}
    def _tasks_for_pkg(self,pkg):
        rows={}
        for group in range(7):
            rows.update({t['id']:{**t,'_pkg':pkg} for t in self.h5_call('replay','/listByType.do')['data']})
        return rows
    def complete_task(self,task,mapping):
        if task['taskComplete']:return {'status':'completed'}
        for path in ('/wish','/web','/complete','/receive'):self.h5_call('replay',path)
        self.finished.add(task['id'])
        self._tasks_for_pkg(task['_pkg'])
        return {'responses':[{'code':0}]}
    def complete_all_tasks(self,packages):
        for pkg in packages:
            for task in self._tasks_for_pkg(pkg).values():self.complete_task(task,{})

packages=['one','two','three']
old=Replay();started=time.perf_counter();old_result=old.complete_all_tasks(packages);old_seconds=time.perf_counter()-started
new=Replay();started=time.perf_counter();new_result=execute_center(new,packages);new_seconds=time.perf_counter()-started
assert old.finished==new.finished==set(range(10))
result={'scope':'offline replay, 10 identical tasks across 3 client identities','real_network_requests':0,
 'before':{'api_calls':old.calls,'list_calls':old.list_calls,'seconds':round(old_seconds,3),'completed':len(old.finished)},
 'after':{'api_calls':new.calls,'list_calls':new.list_calls,'seconds':round(new_seconds,3),'completed':len(new.finished)},
 'api_call_reduction_percent':round((1-new.calls/old.calls)*100,1)}
folder=Path(__file__).resolve().parents[1]/'artifacts';folder.mkdir(exist_ok=True)
(folder/'task-center-benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
