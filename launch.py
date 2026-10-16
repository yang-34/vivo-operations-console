"""Windows launcher. Starts the local app and opens the browser after readiness."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT/'.venv'/'Scripts'/'python.exe'


def health(url):
    try:
        with urllib.request.urlopen(url+'/login',timeout=1) as response:
            return response.headers.get('X-Vivo-Service')=='vivo-operations-console'
    except Exception:
        return False


def main():
    parser=argparse.ArgumentParser(description='Start vivo management console')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    os.chdir(ROOT)
    os.environ['PYTHONIOENCODING']='utf-8'
    if not PYTHON.exists():
        subprocess.run([sys.executable,'-m','venv',str(ROOT/'.venv')],check=True)
    if Path(sys.executable).resolve()!=PYTHON.resolve():
        return subprocess.call([str(PYTHON),str(Path(__file__).resolve()),*sys.argv[1:]],cwd=ROOT)
    if not all(importlib.util.find_spec(m) for m in ('flask','flask_login','flask_limiter','requests','cryptography','Crypto','socks','waitress')):
        subprocess.run([str(PYTHON),'-m','pip','install','-r','requirements.lock.txt'],check=True)
    if not (ROOT/'dist'/'index.html').is_file():
        subprocess.run(['cmd.exe','/d','/c','npm','ci','--no-audit','--no-fund'],cwd=ROOT,check=True)
        subprocess.run(['cmd.exe','/d','/c','npm','run','build'],cwd=ROOT,check=True)
    url='http://127.0.0.1:'+os.environ.get('VIVO_WEB_PORT','8100')
    if health(url):
        print('The vivo console is already running: '+url,flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    ready_stop=threading.Event()
    def open_when_ready():
        for _ in range(100):
            if ready_stop.is_set():
                return
            if health(url):
                if not args.no_browser:
                    webbrowser.open(url)
                return
            ready_stop.wait(.2)
    threading.Thread(target=open_when_ready,daemon=True).start()
    print('vivo console: '+url+'\nPress Ctrl+C to stop.',flush=True)
    try:
        return subprocess.call([str(PYTHON),str(ROOT/'server'/'app.py')],cwd=ROOT)
    except KeyboardInterrupt:
        return 0
    finally:
        ready_stop.set()


if __name__=='__main__':
    sys.exit(main())
