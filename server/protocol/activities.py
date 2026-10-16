"""Activity discovery from server task links and site configuration, without guessing IDs."""
from __future__ import annotations
import json,re
from urllib.parse import unquote
from .common import WUKONGAPI,H5_ORIGIN,POINTH5
from .tasks import discover,route_task,response_info,truth


def discover_catalog(api,packages):
    api.task_inventory={pkg:discover(api,pkg) for pkg in dict.fromkeys(packages)}
    rows=[]
    for pkg,tasks in api.task_inventory.items():
        for task in tasks.values():
            route,reason=route_task(task)
            rows.append({'id':task['id'],'name':task.get('name'),'pkg':pkg,'route':route,'status':'manual' if route=='manual' else 'pending','reason':reason or '已纳入执行清单'})
    return {'packages':len(api.task_inventory),'discovered':len(rows),'results':rows,'status':'completed'}


def configured_activities(value):
    """Keep component context with each ID; one site can contain several distinct families."""
    found={}
    def visit(node,path='',context=''):
        if isinstance(node,str):
            try:visit(json.loads(unquote(node)),path,context)
            except (ValueError,TypeError,RecursionError):pass
        elif isinstance(node,list):
            for child in node:visit(child,path,context)
        elif isinstance(node,dict):
            plugin=node.get('pluginInfo') or {}
            context=context+' '+str(plugin.get('enName') or node.get('pluginEnName') or '')
            if node.get('activityId'):
                aid=str(node['activityId']);hint=(path+' '+context+' '+str(node.get('type',''))+' '+str(node.get('name',''))).lower()
                kinds=found.setdefault(aid,set())
                for kind,terms in {'signin':('signin','sign-in','sign-front'),'lottery':('lottery','draw','squaredpaper'),'tasks':('task',),'lucky':('carveupluck','carveup-luck','luckynumber'),'question':('answer-front',)}.items():
                    if any(term in hint for term in terms):kinds.add(kind)
                if str(node.get('activityType'))=='13':kinds.add('signin')
                if str(node.get('activityType'))=='5':kinds.add('lottery')
                if str(node.get('activityType'))=='8':kinds.add('question')
            for key,child in node.items():visit(child,path+'/'+str(key),context)
    visit(value)
    return found


def site_config(api,site):
    if not hasattr(api,'site_configs'):api.site_configs={}
    if site in api.site_configs:return api.site_configs[site]
    key='activity-config:'+site
    cached=api.s.cache_get(key) if getattr(api.s,'cache_get',None) else None
    if cached is not None:api.site_configs[site]=cached;return cached
    response=api.s.get(WUKONGAPI+'/wukongtopic/site/online/query',params={'siteId':site,'noTimestamp':'true'},headers=api._h5_headers(extra={'Referer':H5_ORIGIN+'/'}),timeout=20).json()
    if api.ok(response) and getattr(api.s,'cache_put',None):api.s.cache_put(key,response)
    api.site_configs[site]=response
    return response


def appointment_family(api,family):
    results=[]
    for appointment in api.NEW_APPOINTMENTS:
        site=appointment['siteId'];config=site_config(api,site)
        for aid,kinds in configured_activities(config).items():
            if family in kinds:results.extend(complete_activity(api,site,aid,{family}))
    return {'results':results} if results else {'skip':'预约页当前未配置'+{'lottery':'抽奖','tasks':'独立任务','comment':'评论抽奖'}.get(family,family)+'组件'}


def question_crypto(api,site,aid):
    result={}
    def visit(node):
        if isinstance(node,str):
            try:visit(json.loads(unquote(node)))
            except (ValueError,TypeError,RecursionError):pass
        elif isinstance(node,list):
            for value in node:visit(value)
        elif isinstance(node,dict):
            if str((node.get('activityInfo') or {}).get('activityId'))==str(aid) and isinstance(node.get('crypto'),dict):result.update(node['crypto'])
            for value in node.values():visit(value)
    visit(site_config(api,site))
    return result


def complete_activity(api,site,aid,kinds):
    p={'siteId':site,'activityId':aid};ref=H5_ORIGIN+'/point/'+site
    results=[]
    def call(path,method='GET',body=None,extra=None):
        return api._wkapc('/wukongapc/'+path,method=method,params={**p,**(extra or {})},body={**p,**(body or {})},referer=ref)
    if 'tasks' in kinds:
        response=call('task/list',extra={'serialNumber':''})
        if api.ok(response):
            tasks=(response.get('data') or {}).get('taskList') or []
            for task in tasks:
                tid=task.get('taskId');key=f'activity:{site}:{aid}:task:{tid}'
                rec={'id':tid,'name':task.get('taskName') or str(tid),'route':'activity','site':site,'activityId':aid}
                if str(task.get('taskStatus'))=='1':
                    results.append({**rec,'status':'completed','reason':'平台已确认完成'});continue
                if re.search('邀请好友|添加导购|购机|购买|支付|充值|订单|核销|晒单|身份认证|分享',str(task.get('taskName') or '')) or str(task.get('taskType'))=='6':
                    results.append({**rec,'status':'manual','reason':'需要用户真实参与、交易或导购操作'});continue
                if truth(task.get('isRemoteTask')):
                    remote=call('task/remote/finish',extra={'locationAuthorizedDTO':json.dumps({'hasUserLocation':False,'province':'','city':''})})
                    rec['response']=response_info(remote)
                else:
                    finished=call('task/finish','POST',{'taskId':tid,'appPkg':task.get('appPkg') or '', 'locationAuthorizedDTO':{'hasUserLocation':False,'province':'','city':''}})
                    rec['response']=response_info(finished)
                results.append({**rec,'status':'pending','reason':'等待任务列表复核'})
            if tasks:
                verified=call('task/list',extra={'serialNumber':''})
                after={str(t.get('taskId')):t for t in (verified.get('data') or {}).get('taskList') or []}
                for rec in results:
                    if rec['status'] in ('completed','manual'):continue
                    complete=str(after.get(str(rec['id']),{}).get('taskStatus'))=='1'
                    rec.update(status='completed' if complete else 'failed',reason='平台复核完成' if complete else '执行后平台未确认完成')
                    if complete and hasattr(api,'checkpoint_save'):api.checkpoint_save(f"activity:{site}:{aid}:task:{rec['id']}")
        else:results.append({'name':'活动任务列表','status':'skipped' if str(response.get('code'))=='1000' else 'failed','response':response_info(response),'site':site,'activityId':aid})
    if 'signin' in kinds:
        response=call('signin/user/info');data=response.get('data') or {}
        if not api.ok(response):results.append({'name':'活动签到','status':'failed','response':response_info(response)})
        elif truth(data.get('todaySignFlag')):results.append({'name':'活动签到','status':'completed','reason':'今日已签到'})
        else:
            signed=call('signin/sign','POST');verified=call('signin/user/info')
            results.append({'name':'活动签到','status':'completed' if truth((verified.get('data') or {}).get('todaySignFlag')) else 'failed','response':response_info(signed)})
    if 'lottery' in kinds:
        for group in (0,1):
            av=call('lottery/user/available/query',extra={'groupId':group} if group else {})
            if not api.ok(av):
                results.append({'name':'免费抽奖次数','status':'skipped' if str(av.get('code')) in ('1000','14001','20001') else 'failed','response':response_info(av)});continue
            data=av.get('data') or {};count=int(data.get('freeAvaCnt') or 0)+int(data.get('taskAvaCnt') or 0)
            if not count:continue
            for _ in range(count):
                response=call('lottery/draw','POST',{'groupId':group,'topicId':site,'extend':{'hostUrl':ref,'locationAuthorizedDTO':{'hasUserLocation':False,'province':'','city':''}}})
                results.append({'name':'免费抽奖','status':'completed' if api.ok(response) else 'failed','reward':(response.get('data') or {}).get('rewardName'),'response':response_info(response)})
                if not api.ok(response):break
            break
    if 'lucky' in kinds:
        av=call('carveupLuck/lucky/available');count=int((av.get('data') or {}).get('availableCount') or 0)
        if count:
            response=call('carveupLuck/lucky/exchange','POST',{'count':count,'hasUserLocation':False,'province':'','city':''})
            results.append({'name':'免费次数兑换幸运码','status':'completed' if api.ok(response) else 'failed','response':response_info(response)})
    if 'question' in kinds:
        if (site,aid) in getattr(api,'answered_activities',set()) or getattr(api,'checkpoint_done',lambda key:False)('question:'+site+':'+aid):results.append({'name':'活动答题','status':'skipped','reason':'已在积分答题步骤处理'})
        else:
            response=api.question(activity_id=aid,site_id=site)
            results.append({'name':'活动答题','status':'skipped' if response.get('skip') else 'failed' if response.get('err') or response.get('_err') else 'completed','reason':response.get('skip',''),'response':response})
    return [{'site':site,'activityId':aid,**row} for row in results]


def execute_dynamic(api,only_sites=None):
    sites=list(only_sites) if only_sites is not None else list(api._wk_site_ids())
    for site in ([] if only_sites is not None else [*(a['siteId'] for a in api.NEW_APPOINTMENTS),api.WEEKLY_SITE,'wk241209756168f3','wk2609233beab7e8','wk240410b3206fb4']):
        if site not in sites:sites.append(site)
    for tasks in ({} if only_sites is not None else getattr(api,'task_inventory',{})).values():
        for task in tasks.values():
            for site in re.findall(r'wk[0-9a-z]{10,16}',unquote(str(task.get('jumpURL') or task.get('href') or ''))):
                if site not in sites:sites.append(site)
    output={'sites':len(sites),'activities':0,'results':[]}
    api.partial_result=output
    for site in sites:
        try:response=site_config(api,site)
        except Exception as exc:
            output['results'].append({'name':site,'status':'failed','reason':'站点配置请求失败：'+type(exc).__name__});continue
        state=(response.get('data') or {}).get('state')
        if state in (4,6):
            output['results'].append({'name':site,'status':'skipped','reason':f'平台站点已下线，state={state}'});continue
        for aid,kinds in configured_activities(response).items():
            if not kinds:
                from .har_tasks import BENEFITS,COUPONS
                if aid.isdigit() and int(aid) in (*BENEFITS,*COUPONS):
                    output['results'].append({'name':'商城礼包或优惠券组件','site':site,'activityId':aid,'status':'skipped','reason':'由“商城新人礼包及会场免费券”步骤独立核验'})
                    continue
                if aid.isdigit() and len(aid)>=10:output['results'].append({'name':site,'site':site,'activityId':aid,'status':'blocked','reason':'活动配置未匹配到已知任务组件，保留待适配，不计为完成'})
                continue
            output['activities']+=1
            try:output['results'].extend(complete_activity(api,site,aid,kinds))
            except Exception as exc:output['results'].append({'name':site,'activityId':aid,'status':'failed','reason':'活动处理异常：'+type(exc).__name__})
    if not output['activities'] and not output['results']:output['skip']='发现的站点没有可识别的免费活动组件'
    return output
