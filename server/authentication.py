"""Mandatory passphrase authentication using Flask-Login and Flask-Limiter."""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path
from flask import jsonify,redirect,request,session,Response
from flask_login import LoginManager,UserMixin,current_user,login_user,logout_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.security import generate_password_hash,check_password_hash
import store

CONFIG_CACHE={}
limiter=None


def config():
    folder=store.DATA
    key=(str(folder),os.environ.get('VIVO_ADMIN_PASSWORD',''),os.environ.get('VIVO_ADMIN_PASSWORD_HASH',''))
    if key in CONFIG_CACHE:return CONFIG_CACHE[key]
    hashed=os.environ.get('VIVO_ADMIN_PASSWORD_HASH')
    if not hashed and os.environ.get('VIVO_ADMIN_PASSWORD'):
        hashed=generate_password_hash(os.environ['VIVO_ADMIN_PASSWORD'],method='scrypt')
    if not hashed:
        hash_file=folder/'.admin-password-hash'
        if not hash_file.exists():
            password=secrets.token_urlsafe(21)
            password_file=folder/'admin-password.txt'
            password_file.write_text(password+'\n',encoding='utf-8');password_file.chmod(0o600)
            hash_file.write_text(generate_password_hash(password,method='scrypt'),encoding='utf-8');hash_file.chmod(0o600)
        hashed=hash_file.read_text(encoding='utf-8').strip()
    result={'hash':hashed,'user_id':'admin:'+hashlib.sha256(hashed.encode()).hexdigest()[:16]}
    CONFIG_CACHE[key]=result
    return result


class Administrator(UserMixin):
    def __init__(self,user_id):self.id=user_id


LOGIN_HTML='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · vivo 运营管理平台</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#263449;font:14px "Segoe UI","Microsoft YaHei",sans-serif;min-height:100vh;display:grid;place-items:center}.login{width:390px;max-width:calc(100vw - 40px);background:white;border:1px solid #e4e9f1;border-radius:8px;padding:38px}.brand{font-size:39px;color:#2855df;font-style:italic;letter-spacing:-3px;font-weight:650}h1{font-size:21px;margin:24px 0 10px;font-weight:600}p{font-size:12px;line-height:1.9;color:#92a0b3;margin:0 0 24px}label{display:block;margin-bottom:9px;font-size:12px;color:#728199}input{width:100%;border:1px solid #dfe5ef;border-radius:5px;height:41px;padding:0 12px;outline:none;font-size:14px}input:focus{border-color:#3564df;box-shadow:0 0 0 2px #edf2ff}button{width:100%;margin-top:20px;height:41px;background:#2855df;border:0;color:white;border-radius:5px;font-size:13px;cursor:pointer}button:disabled{opacity:.6}.error{min-height:20px;margin-top:13px;font-size:12px;color:#ce5c66}.foot{margin-top:19px;border-top:1px solid #edf0f5;padding-top:19px;font-size:10px;color:#a9b2c0}
</style></head><body><form class="login" id="form"><div class="brand">vivo<span style="font-size:20px">+</span></div><h1>登录管理平台</h1><p>输入管理口令后，访问账号、任务与代理工作区。</p><label for="password">管理口令</label><input id="password" type="password" autocomplete="current-password" placeholder="请输入管理口令" required autofocus><button id="submit" type="submit">登录</button><div class="error" id="error" role="alert"></div><div class="foot">vivo 运营管理平台 · 受保护的工作区</div></form><script>
document.getElementById('form').addEventListener('submit',async e=>{e.preventDefault();const b=document.getElementById('submit');b.disabled=true;document.getElementById('error').textContent='';try{const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('password').value})});const d=await r.json();if(!r.ok)throw new Error(d.error||'登录失败');document.getElementById('password').value='';location.replace('/')}catch(err){document.getElementById('error').textContent=err.message}finally{b.disabled=false}})
</script></body></html>'''


def install(app):
    global limiter
    config()
    secret_file=store.DATA/'.web-session-key'
    if not secret_file.exists():secret_file.write_bytes(secrets.token_bytes(48));secret_file.chmod(0o600)
    app.secret_key=os.environ.get('VIVO_SESSION_SECRET') or secret_file.read_bytes()
    app.config.update(SESSION_COOKIE_NAME='vivo_session',SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Strict',
      SESSION_COOKIE_SECURE=os.environ.get('VIVO_SECURE_COOKIE','0')=='1',PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
    manager=LoginManager(app);manager.session_protection='strong'
    @manager.user_loader
    def load_user(user_id):return Administrator(user_id) if hmac.compare_digest(user_id,config()['user_id']) else None
    limiter=Limiter(get_remote_address,app=app,storage_uri=os.environ.get('VIVO_RATE_LIMIT_STORAGE','memory://'),default_limits=[])

    @app.before_request
    def require_authentication():
        if request.endpoint in ('login_page','auth_login'):return
        if not current_user.is_authenticated:
            if request.path.startswith('/api/') or request.path.startswith('/assets/'):
                return jsonify(error='请先输入管理口令'),401
            return redirect('/login')
        if request.method in ('POST','PATCH','DELETE','PUT'):
            supplied=request.headers.get('X-CSRF-Token','')
            if not supplied or not hmac.compare_digest(supplied,session.get('csrf','')):
                return jsonify(error='会话校验失败，请刷新后重试'),403

    @app.after_request
    def security_headers(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='SAMEORIGIN' if request.endpoint=='commerce.captcha_widget' else 'DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        if request.endpoint not in ('login_page','commerce.captcha_widget'):
            response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: http: data:; font-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        if request.is_secure:response.headers['Strict-Transport-Security']='max-age=31536000'
        response.headers['X-Vivo-Service']='vivo-operations-console'
        return response

    @app.get('/login')
    def login_page():
        if current_user.is_authenticated:return redirect('/')
        return Response(LOGIN_HTML,mimetype='text/html')

    @app.post('/api/auth/login')
    @limiter.limit('5 per minute',deduct_when=lambda response:response.status_code==401)
    def auth_login():
        password=str((request.get_json(silent=True) or {}).get('password') or '')
        if len(password)>1024 or not check_password_hash(config()['hash'],password):return jsonify(error='管理口令不正确'),401
        session.clear();login_user(Administrator(config()['user_id']),fresh=True)
        session.permanent=True;session['csrf']=secrets.token_urlsafe(32)
        return jsonify(ok=True,csrf_token=session['csrf'])

    @app.get('/api/auth/session')
    def auth_session():return jsonify(authenticated=True,csrf_token=session.get('csrf'))

    @app.post('/api/auth/logout')
    def auth_logout():logout_user();session.clear();return jsonify(ok=True)

    @app.errorhandler(429)
    def login_limited(error):return jsonify(error='口令尝试过于频繁，请稍后再试'),429
