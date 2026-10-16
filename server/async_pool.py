"""Persistent processes with bounded asyncio slots for blocking protocol clients."""
from __future__ import annotations
import asyncio
import importlib
import multiprocessing
import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future,ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool


def worker_entry(data_directory,slots,function_name,jobs,results):
    from pathlib import Path
    import store,engine
    store.DATA=Path(data_directory);engine.DATA=store.DATA
    module,name=function_name.split(':',1)
    function=getattr(importlib.import_module(module),name)
    parent=multiprocessing.parent_process();pid=os.getpid();started=store.now()
    running={slot:None for slot in range(slots)};stop=threading.Event()
    def heartbeat():
        while not stop.is_set():
            if parent is not None and not parent.is_alive():os._exit(0)
            try:
                snapshot=list(running.items());active=[rid for _,rid in snapshot if rid is not None]
                with store.db('live') as c:
                    c.execute('INSERT OR REPLACE INTO worker_status VALUES(?,?,?,?,?)',(pid,'busy' if active else 'idle',active[0] if len(active)==1 else None,time.time(),started))
                    c.executemany('INSERT OR REPLACE INTO worker_slots VALUES(?,?,?,?)',[(pid,slot,rid,time.time()) for slot,rid in snapshot])
            except Exception:pass
            stop.wait(2)
    threading.Thread(target=heartbeat,daemon=True,name='worker-heartbeat').start()
    async def slot_loop(slot):
        while True:
            job=await asyncio.to_thread(jobs.get)
            if job is None:return
            job_id,args=job;running[slot]=args[1] if len(args)>1 else None
            results.put(('started',job_id,pid,None))
            try:
                value=await asyncio.to_thread(function,*args)
                results.put(('done',job_id,value,None))
            except Exception:
                # Exceptions can contain authenticated URLs; keep them out of IPC/error logs.
                results.put(('failed',job_id,None,'账号执行异常，已保留断点'))
            finally:running[slot]=None
    async def main():
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=slots,thread_name_prefix='account-io'))
        await asyncio.gather(*(slot_loop(slot) for slot in range(slots)))
    try:asyncio.run(main())
    finally:
        stop.set()
        try:
            with store.db('live') as c:
                c.execute('DELETE FROM worker_slots WHERE pid=?',(pid,))
                c.execute('DELETE FROM worker_status WHERE pid=?',(pid,))
        except Exception:pass


class AsyncProcessPool:
    def __init__(self,processes,slots,data_directory,function_name='executor:process_run'):
        self.slots=slots;self.capacity=processes*slots
        self.lock=threading.Lock();self.pending={};self.closed=False;self.broken=False
        context=multiprocessing.get_context('spawn')
        self.jobs=context.Queue();self.results=context.Queue()
        self.processes=[context.Process(target=worker_entry,args=(data_directory,slots,function_name,self.jobs,self.results),name='vivo-worker') for _ in range(processes)]
        for process in self.processes:process.start()
        threading.Thread(target=self._receive,daemon=True,name='worker-results').start()

    def submit(self,function,*args):
        future=Future();key=uuid.uuid4().hex
        with self.lock:
            if self.closed or self.broken:raise BrokenProcessPool('执行进程池不可用')
            self.pending[key]=future
            self.jobs.put((key,args))
        return future

    def _receive(self):
        while not self.closed and not self.broken:
            try:event,key,value,error=self.results.get(timeout=.5)
            except queue.Empty:
                if any(process.exitcode is not None for process in self.processes):
                    self.abort()
                    return
                continue
            if event=='started':continue
            with self.lock:future=self.pending.pop(key,None)
            if future and not future.done():
                if error:future.set_exception(RuntimeError(error))
                else:future.set_result(value)

    def idle(self):
        with self.lock:return not self.pending

    def abort(self):
        with self.lock:
            if self.broken:return
            self.broken=True
        for process in self.processes:
            if process.is_alive():process.terminate()
        for process in self.processes:process.join(timeout=3)
        with self.lock:pending=list(self.pending.values());self.pending.clear()
        for future in pending:
            if not future.done():future.set_exception(BrokenProcessPool('执行进程退出，等待从断点恢复'))

    def shutdown(self,wait=True,cancel_futures=False):
        if self.closed:return
        if wait:
            while not self.idle() and not self.broken:time.sleep(.05)
        self.closed=True
        if not self.broken:
            for _ in range(self.capacity):self.jobs.put(None)
        if wait:
            for process in self.processes:
                process.join(timeout=5)
                if process.is_alive():process.terminate();process.join(timeout=2)
        self.jobs.close()
