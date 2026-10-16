"""Extract protocol shapes from an authorized HAR without exporting session secrets."""
import base64,collections,json,re,sys
from pathlib import Path
from urllib.parse import urlparse,parse_qsl
ROOT=Path(__file__).resolve().parents[1]
SECRET=re.compile(r'token|cookie|password|secret|openid|unionid|imei|oaid|vaid|aaid|phone|mobile|authorization|sign(?:ature)?$|ticket|const.?id|couponcode|redeemcode|cardpwd|userid',re.I)
def scrub(value):
    if isinstance(value,dict):return {k if len(k)<100 else '[opaque key]':'[redacted]' if SECRET.search(k) or len(k)>=100 else scrub(v) for k,v in value.items()}
    if isinstance(value,list):return [scrub(v) for v in value[:80]]
    if isinstance(value,str):
        if len(value)>300:return value[:80]+'[long value omitted]'
    return value
def response(entry):
    content=entry.get('response',{}).get('content',{});text=content.get('text','')
    try:
        if content.get('encoding')=='base64':text=base64.b64decode(text).decode('utf-8')
        return json.loads(text)
    except (ValueError,UnicodeError):return None
def body(request):
    post=request.get('postData',{})
    try:return json.loads(post.get('text',''))
    except ValueError:
        return dict(parse_qsl(post.get('text',''))) or {v['name']:v.get('value') for v in post.get('params',[])}

def cookie_names(request):
    """HAR can put its full cookie jar outside a partial Cookie header."""
    names={item['name'] for item in request.get('cookies',[]) if item.get('name')}
    for header in request.get('headers',[]):
        if header.get('name','').lower()=='cookie':
            names.update(part.strip().split('=',1)[0] for part in header.get('value','').split(';') if '=' in part)
    return sorted(names)
if __name__=='__main__':
    har=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
    groups=collections.defaultdict(list);devices=[];origins=collections.Counter()
    for index,entry in enumerate(har['log']['entries']):
        req=entry['request'];url=urlparse(req['url']);origins[url.hostname]+=1
        if not url.hostname or not any(domain in url.hostname for domain in ('vivo','iqoo')):continue
        query=dict(parse_qsl(url.query));payload=body(req);value=response(entry)
        headers={h['name'].lower():h['value'] for h in req.get('headers',[])}
        ua=headers.get('user-agent','')
        ua=re.sub(r'(?i)(imei|oaid|vaid|aaid|gitId|u)=[^& ]*',r'\1=[redacted]',ua)
        if ua and ua not in devices:devices.append(ua)
        if any(x in url.path.lower() for x in ('/api/','/sdk/','/wukong','/home/','/client/','/service/')):
            groups[(req['method'],url.hostname,url.path)].append({'index':index,'query':scrub(query),'body':scrub(payload),'cookie_names':cookie_names(req),'http_status':entry.get('response',{}).get('status'),'response':scrub(value)})
    result={'total_entries':len(har['log']['entries']),'origins':dict(origins),'user_agents':devices,'contracts':[{'method':m,'host':h,'path':p,'count':len(rows),'samples':rows[:3]} for (m,h,p),rows in groups.items()]}
    out=ROOT/'data'/'har-contracts-redacted.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'entries':result['total_entries'],'contracts':len(result['contracts']),'output':str(out)},ensure_ascii=False))
