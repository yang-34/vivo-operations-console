"""Discover grouped tasks once, deduplicate identical writes, verify state centrally."""
from __future__ import annotations
import json
import re
import time
from .common import POINTH5,POINTSDK


def flatten_tasks(value,pkg):
    out={}
    def visit(node):
        if isinstance(node,list):
            for item in node:visit(item)
        elif isinstance(node,dict):
            if node.get('id') is not None and ('name' in node or 'taskComplete' in node or 'completedTimes' in node):
                out[node['id']]={**node,'_pkg':pkg}
            else:
                for key in ('data','tasks','list','items'):
                    if key in node:visit(node[key])
    visit(value)
    return out


def discover(api,pkg):
    response=api.h5_call(POINTH5,'/api/task/list.do',method='POST',params={},pkg=pkg)
    if str(response.get('retcode',response.get('code'))) in ('0','200'):
        tasks=flatten_tasks(response,pkg)
        groups=(response.get('data') or {}).get('tasks',[]) if isinstance(response.get('data'),dict) else []
        # Fetch only groups explicitly marked as truncated; no 0..6 blind enumeration.
        for group in groups:
            if isinstance(group,dict) and str(group.get('hasMore')).lower() in ('true','1'):
                tasks.update(flatten_tasks(api.h5_call(POINTH5,'/api/task/listByType.do',params={'type':str(group['type'])},pkg=pkg),pkg))
        return tasks
    return api._tasks_for_pkg(pkg)


def identity(task):
    # Cookie package now follows the task's actual business. Never merge writes across those contexts.
    return tuple(str(task.get(k,'')) for k in ('id','name','type','subType','taskSource','businessCode','keyId'))+(str(task.get('_pkg','')) if task.get('keyId') or task.get('businessCode') else '',)


def is_complete(task):
    if 'taskComplete' in task:return task.get('taskComplete') is True or str(task.get('taskComplete')).lower() in ('true','1')
    # EC responses omit taskComplete; their documented completion state is the progress pair.
    return task.get('totalTime') is not None and int(task.get('totalTime') or 0)>0 and int(task.get('completedTimes') or 0)>=int(task['totalTime'])


def truth(value):
    return value is True or str(value).lower() in ('1','true')


def unclaimed(task):
    return int(task.get('unreceviedPoints') or task.get('unreceivedPoints') or 0)


def response_info(response):
    result={'code':response.get('retcode',response.get('code')),'message':response.get('message') or response.get('msg') or response.get('_err') or ''}
    if isinstance(response.get('data'),dict) and response['data'].get('rewardPoints') is not None:result['reward_points']=response['data']['rewardPoints']
    return result


def route_task(task):
    name=str(task.get('name') or '')
    if truth(task.get('signTask')) or task.get('taskCatg')=='sign' or '签到' in name:return 'sign',''
    for phrase,route in [('加入购物车','shop_cart'),('浏览商品详情','shop_detail'),('搜索1款商品','shop_search'),('访问会员中心','shop_member')]:
        if phrase in name:return route,''
    if '身份认证' in name:return 'manual','需要本人完成真实学生/教师身份认证'
    if '分享商品' in name:return 'manual','需要本人通过官方客户端选择分享渠道和接收对象'
    if any(word in name for word in ('绑卡','借钱','开通额度')):return 'manual','需要本人在钱包完成银行业务授权或绑卡，不能代为开通'
    if '搜索' in name:return 'manual','需要在对应 vivo 客户端完成真实搜索行为；当前账号未提供原生搜索会话'
    if any(word in name for word in ('观看','订阅红包','推送')):return 'manual','需要对应 vivo 客户端会话与本人操作授权'
    # These requirements are stated by the platform task descriptions, not inferred completion.
    if re.search('购机|购物|购买|消费|贷款|推荐配件|推荐.*送积分',name):return 'manual','需要真实交易、推荐订单或金融操作'
    if '评价' in name or '发帖' in name:return 'manual','需要用户真实评价或发布内容'
    if '会员等级升至' in name:return 'eligibility','会员等级尚未达到平台要求'
    if str(task.get('style'))=='2':return 'activity','活动推广入口，按站点活动配置执行；未提供有效活动地址时保留待处理'
    if str(task.get('taskSource'))=='3':return 'manual','需要门店导购核销'
    if '阅读' in name and '帖子' in name:return 'community',''
    if '收藏' in name and '商品' in name:return 'favorite',''
    if str(task.get('type'))=='9':return 'ad',''
    if truth(task.get('webTask')):return 'web',''
    if task.get('subKeyId') is not None and task.get('keyId'):return 'vivop',''
    if task.get('actionId'):return 'sdk',''
    if not task.get('keyId') and task.get('jumpURL'):return 'activity','活动页由动态活动任务执行器处理'
    if '心愿' in name or '许愿' in name:return 'wish',''
    return 'manual','平台未提供已适配的实际操作协议；保留待处理，不将许愿回执当作完成'


def perform_task(api,task,mapping):
    """Follow the advertised task contract, collect rewards, then let the caller verify state."""
    route,reason=route_task(task);pkg=task.get('_pkg') or 'com.vivo.space'
    rec={'id':task['id'],'name':task.get('name'),'pkg':pkg,'route':route,'responses':[]}
    def record(response):
        rec['responses'].append(response_info(response));return response
    if is_complete(task) and not unclaimed(task):return {**rec,'status':'completed','reason':'平台已确认完成'}
    if route=='manual' and not unclaimed(task):return {**rec,'status':'manual','reason':reason}
    if route=='eligibility' and not unclaimed(task):return {**rec,'status':'blocked','reason':reason}
    if not unclaimed(task) and not is_complete(task):
        if route=='sign':
            biz=task.get('bizCode') or task.get('businessCode') or api._biz_for_name(task.get('name'),mapping)
            if not biz:return {**rec,'status':'blocked','reason':'未匹配到服务端签到业务'}
            record(api.sign_do(biz,pkg=pkg))
        elif route=='web':
            key=task.get('taskKeyId') or task.get('keyId')
            if not key:return {**rec,'status':'blocked','reason':'网页任务未下发 taskKeyId'}
            record(api.complete_web_task(key,pkg=pkg))
        elif route=='community':
            count=re.search(r'(\d+)',task.get('name') or '')
            result=api.community_read(int(count[1]) if count else 3)
            rec['action']=result
        elif route=='favorite':rec['action']=api.mall_favorite()
        elif route.startswith('shop_'):
            from .har_tasks import shop_action
            action=shop_action(api,route)
            rec['action']=action
            if action.get('status') in ('blocked','failed'):return {**rec,'status':action['status'],'reason':action.get('reason','商城前置动作未完成')}
        elif route=='sdk':
            record(api._sdk_upload(pkg,task['actionId'],max(1,int(task.get('totalTime') or 1)-int(task.get('completedTimes') or 0))))
            record(api._sdk_receive(pkg,task['actionId']))
        elif route=='vivop':record(api.vivop_complete(task['keyId'],task.get('subKeyId'),task.get('businessCode') or 'vivop'))
        elif route=='ad':
            app=task.get('app') or {}
            if not (app.get('appId') and task.get('traceId')):return {**rec,'status':'manual','reason':'需要客户端真实广告 appId / traceId'}
            record(api.h5_call(POINTH5,'/api/cpd/task/app/report.do',method='POST',pkg=pkg,params={'eventBizType':6,'eventBizId':task['id'],'appId':app['appId'],'traceId':task['traceId'],'reportType':2}))
        elif route=='wish':record(api.task_wish(task['id'],pkg=pkg))
        elif route=='activity':return {**rec,'status':'blocked','reason':reason,'entry':task.get('jumpURL') or task.get('href') or ''}
    # Completed tasks may still have points waiting to be claimed.
    if route=='vivop':
        if not task.get('vivopTaskId'):return {**rec,'status':'blocked','reason':'平台未下发 vivopTaskId，保留待领取'}
        params={k:task[k] for k in ('keyId','subKeyId','businessCode','vivopTaskId','vivopCostParam','duration') if k in task}
        claim=record(api.h5_call(POINTH5,'/api/task/vivop/receivePoint.do',method='POST',pkg=pkg,params=params))
    else:claim=record(api.ec_receive_point(task) if task.get('businessCode') or task.get('_ec') else api.receive_point(task))
    rec['claim_accepted']=api.ok(claim)
    rec['status']='pending'
    rec['reason']=reason or (response_info(claim)['message'] if not api.ok(claim) else '已提交，等待平台状态核对')
    return rec


def execute_center(api,packages,checkpoint=None):
    snapshots={pkg:discover(api,pkg) for pkg in packages}
    unique={}
    for snapshot in snapshots.values():
        for task in snapshot.values():
            unique.setdefault(identity(task),task)
    mapping=api.sign_biz_names()
    pending={key:task for key,task in unique.items() if not is_complete(task) or unclaimed(task)}
    tally={'discovered':sum(map(len,snapshots.values())),'unique':len(unique),'already':len(unique)-len(pending),'completed':0,'no_change':0,'progressed':0,'manual':0,'blocked':0,'results':[]}
    api.partial_result=tally
    for task in unique.values():
        if is_complete(task) and not unclaimed(task):tally['results'].append({'id':task['id'],'name':task.get('name'),'pkg':task['_pkg'],'status':'completed','reason':'平台已确认完成'})
    records={}
    original=api._tasks_for_pkg
    while pending:
        for key,task in list(pending.items()):
            # Suppress the legacy full-list reload at the end of every single task.
            api._tasks_for_pkg=lambda pkg:snapshots.get(pkg,{})
            try:records[key]=api.complete_task(task,mapping) or {}
            finally:api._tasks_for_pkg=original
        affected={task['_pkg'] for key,task in pending.items() if not records[key] or records[key].get('responses') or records[key].get('action')}
        after={**snapshots,**{pkg:discover(api,pkg) for pkg in affected}}
        # Only poll known completed actions. Never repeat a mutation while its response is uncertain.
        for _ in range(getattr(api,'verify_poll_count',0)):
            waiting={task['_pkg'] for key,task in pending.items() if records[key].get('claim_accepted') and records[key].get('route') in ('web','sign','community','favorite','vivop') and not is_complete(after.get(task['_pkg'],{}).get(task['id'],{}))}
            if not waiting:break
            time.sleep(.5)
            after.update({pkg:discover(api,pkg) for pkg in waiting})
        remaining={}
        for key,task in pending.items():
            candidates=[t for snapshot in after.values() for t in snapshot.values() if identity(t)==key]
            latest=next((t for t in candidates if is_complete(t)),candidates[0] if candidates else {})
            rec={**records.get(key,{}),'id':task['id'],'name':task.get('name'),'pkg':task['_pkg'],'before':task.get('completedTimes'),'after':latest.get('completedTimes'),'unclaimed':unclaimed(latest)}
            if is_complete(latest) and not unclaimed(latest):
                tally['completed']+=1
                rec.update(status='completed',reason='重新查询确认完成，待领积分已清空')
                if checkpoint:checkpoint('task:'+json.dumps(key,ensure_ascii=False),'completed')
            elif rec.get('status')=='manual':
                tally['manual']+=1
            elif rec.get('status')=='blocked':
                tally['blocked']+=1
            elif latest and int(latest.get('completedTimes') or 0)>int(task.get('completedTimes') or 0):
                remaining[key]=latest;continue
            else:
                tally['no_change']+=1;rec['status']='blocked' if rec.get('route')=='eligibility' else 'failed'
                rec['reason']=rec.get('reason') or '执行后平台任务状态未变化，未计为完成'
            tally['results'].append(rec)
        pending=remaining;snapshots=after
    return tally


def execute_sdk_package(api,pkg,completed=None,save=None):
    config=api.h5_call(POINTSDK,'/sdk/config/action/list',method='POST',pkg=pkg,params={'pkgName':pkg})
    if not api.ok(config):return {'failed':1,'note':'当前业务 SDK 配置不可用','results':[{'name':pkg,'status':'blocked','reason':response_info(config)['message'] or '当前业务 SDK 配置不可用','response':response_info(config)}]}
    actions=(config.get('data') or {}).get('actions') or []
    summary={'actions':len(actions),'uploaded':0,'points_claimed':0,'checkpoint_skipped':0,'unavailable':0,'not_applicable':0,'failed':0,'results':[]}
    api.partial_result=summary
    receipts=[]
    for action in actions:
        aid=action.get('actionId')
        if not aid:continue
        key=f'sdk:{pkg}:{aid}'
        rec={'id':aid,'name':action.get('actionName') or action.get('name') or str(aid),'pkg':pkg,'route':'sdk'}
        if completed and completed(key):
            summary['checkpoint_skipped']+=1;summary['results'].append({**rec,'status':'completed','reason':'此前已确认领取'});continue
        deferred=getattr(api,'checkpoint_deferred',lambda key:0)(key)
        if deferred>time.time():
            summary['unavailable']+=1;summary['results'].append({**rec,'status':'blocked','reason':'平台要求暂停上报，等待指定时间再执行','retry_at':deferred});continue
        uploaded=api._sdk_upload(pkg,aid,1 if pkg=='com.vivo.space' else 5)
        summary['uploaded']+=int(api.ok(uploaded))
        rec['upload']=response_info(uploaded)
        data=uploaded.get('data') or {};notice=data.get('sdkTaskNotifyVo') or {}
        rec['task_id']=data.get('taskId') or notice.get('taskId')
        rec['upload_control']={k:data[k] for k in ('disableUpload','disableUntil') if k in data}
        if notice.get('notifyContent'):rec['name']=re.sub('<[^>]*>','',notice['notifyContent'])
        if not api.ok(uploaded):
            summary['failed']+=1;summary['results'].append({**rec,'status':'failed','reason':'SDK 上报未被平台接受'});continue
        if not notice and (not data.get('taskId') or pkg=='com.vivo.space' and hasattr(api,'_native_sdk')):
            summary['not_applicable']+=1
            rec.update(status='skipped',reason='官方未下发领奖通知，本次只有行为回执；未请求领取或计为积分到账')
            summary['results'].append(rec);continue
        until=float(data.get('disableUntil') or 0)
        if until>10**11:until/=1000
        if truth(data.get('disableUpload')) and until>time.time() and hasattr(api,'checkpoint_defer'):api.checkpoint_defer(key,until)
        # The SDK schedules receipt after the advertised delay. Upload other actions while waiting.
        due=time.monotonic()+max(0,float(notice.get('delay') or 0))/1000
        rec.update(status='pending',reason='行为已上报，等待平台要求的领奖时机')
        summary['results'].append(rec)
        receipts.append((due,aid,key,rec,uploaded,notice.get('actionId') or aid))
    for due,aid,key,rec,uploaded,receive_id in sorted(receipts,key=lambda item:item[0]):
        while time.monotonic()<due:
            if time.monotonic()>getattr(getattr(api,'s',None),'deadline',float('inf')):
                from .transport import StepTimeout
                raise StepTimeout('SDK 领奖延时超过本步骤时限，已保留已领取断点')
            time.sleep(min(1,due-time.monotonic()))
        received=api._sdk_receive(pkg,receive_id)
        if api.ok(received):
            summary['points_claimed']+=1
            rec.update(status='completed',reason='平台确认 SDK 领奖成功')
            if save:save(key)
        elif str(received.get('code',received.get('retcode')))=='10002' and '不存在' in str(received.get('message') or received.get('msg') or ''):
            summary['not_applicable']+=1
            rec.update(status='skipped',reason='平台明确返回该行为没有积分任务')
        elif uploaded.get('_err') or received.get('_err'):
            summary['failed']+=1
            rec.update(status='failed',reason='SDK 上报或领奖请求失败')
        else:
            summary['unavailable']+=1
            rec.update(status='blocked',reason='平台未确认 SDK 领奖，保留待核对')
        rec['receive']=response_info(received)
    return summary


def execute_ec(api):
    from .common import PN_MAP
    output={'discovered':0,'completed':0,'earned':0,'results':[]}
    api.partial_result=output
    api.ec_inventory={}
    for pkg in dict.fromkeys(PN_MAP.values()):
        try:tasks=api._ec_list(pkg)
        except Exception as error:
            output['results'].append({'name':pkg,'status':'failed','reason':'EC 目录查询失败：'+type(error).__name__})
            continue
        api.ec_inventory[pkg]=tasks
        output['discovered']+=len(tasks)
        pending=[]
        for task in tasks:
            t={**task,'_pkg':pkg,'_ec':True}
            if not truth(t.get('webTask')) and not unclaimed(t):
                if is_complete(t) or not route_task(t)[0].startswith('shop_'):
                    output['results'].append({'id':t.get('id'),'name':t.get('name'),'pkg':pkg,'status':'completed' if is_complete(t) else 'blocked','reason':'平台已确认完成' if is_complete(t) else route_task(t)[1] or '需要原生客户端实际行为，继续通过对应任务处理器核验'})
                    continue
            if is_complete(t) and not unclaimed(t):
                output['results'].append({'id':t['id'],'name':t.get('name'),'pkg':pkg,'status':'completed','reason':'平台已确认完成'});continue
            rec=perform_task(api,t,{})
            output['earned']+=0 # Balance reconciliation is authoritative; never infer points from upload success.
            pending.append((t,rec))
        if not pending:continue
        after={str(t.get('id')):t for t in api._ec_list(pkg)}
        for task,rec in pending:
            latest=after.get(str(task['id']),{})
            claimed=unclaimed(task)>0 and unclaimed(latest)==0 and rec.get('claim_accepted')
            complete=is_complete(latest) and not unclaimed(latest) or claimed
            rec.update(status='completed' if complete else 'failed',reason='待领积分已结算' if claimed else '平台复核完成' if complete else '网页操作后平台任务状态未达成',before=task.get('completedTimes'),after=latest.get('completedTimes'))
            output['completed']+=int(complete);output['results'].append(rec)
    if not output['results']:output['skip']='当前城市与业务未下发可执行的网页任务'
    return output
