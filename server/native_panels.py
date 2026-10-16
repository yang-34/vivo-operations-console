"""Configured application-bao endpoints; no default addresses or reference checkout."""
import concurrent.futures
import requests


def iter_events(payload):
    sources=payload.get('panel_sources',[])
    yield {'type':'pool_start','total':len(sources)}
    def fetch(index,source):
        url=source['url'].rstrip('/')
        if url.lower().startswith('http://') and payload.get('proxy_required') and not payload.get('allow_http_panels'):
            return {'type':'pool_error','pool_index':index+1,'pool_url':url,'message':'HTTP 面板未授权'}
        session=requests.Session();session.trust_env=False;session.proxies.update(payload.get('proxy') or {})
        try:
            response=session.get(url+'/accounts',headers={'apiKey':source.get('api_key','')} if source.get('api_key') else {},timeout=(4,8))
            response.raise_for_status();body=response.json()
            if str(body.get('code'))!='0' or not isinstance(body.get('data'),list):raise ValueError('面板响应格式错误')
            items=[];online=0
            for account in body['data']:
                ref=str(account.get('openid') or account.get('uin') or account.get('id') or '')
                if not ref:raise ValueError('账号缺少标识')
                state=str(account.get('status') or '').lower()
                is_online=state in ('alive','ok','online','')
                online+=int(is_online)
                items.append({'openid':'wx:'+ref,'_wx_openid':ref,'_pool':url,'nickname':str(account.get('nickname') or account.get('display_name') or ref[-8:]),'source':source.get('label') or f'应用宝面板 {index+1:03}','panel_status':'online' if is_online else 'offline'})
            return {'type':'pool_accounts','pool_index':index+1,'pool_url':url,'items':items,'online':online,'offline':len(items)-online}
        except Exception as exc:
            return {'type':'pool_error','pool_index':index+1,'pool_url':url,'message':type(exc).__name__+'：面板连接或读取失败'}
        finally:session.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(12,max(1,len(sources)))) as executor:
        for future in concurrent.futures.as_completed([executor.submit(fetch,i,s) for i,s in enumerate(sources)]):yield future.result()
    yield {'type':'pool_end'}
