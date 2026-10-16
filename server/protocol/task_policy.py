"""Reversible exclusions for tasks that cannot currently be completed automatically."""
import copy
import json
import re
from functools import lru_cache
from pathlib import Path

import store


@lru_cache(maxsize=8)
def _read(path, modified):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value,dict) or any(not isinstance(value.get(k,{}),dict) for k in ('steps','task_keys','task_names')):
        raise ValueError('临时停用任务配置格式无效')
    return value


def policy():
    path=store.DATA/'task-exclusions.json'
    try:return _read(str(path),path.stat().st_mtime_ns)
    except FileNotFoundError:return {}


def step_reason(key):
    return policy().get('steps',{}).get(key,'')


def task_reason(key, handler=None):
    rules=policy()
    return rules.get('task_keys',{}).get(key,'') or rules.get('steps',{}).get(handler or 'source.'+key,'')


def ready(task):
    try:
        if float(task.get('unreceviedPoints') or task.get('unreceivedPoints') or 0)>0:return True
        if 'taskComplete' in task:return str(task.get('taskComplete')).lower() in ('true','1')
        return float(task.get('totalTime') or 0)>0 and float(task.get('completedTimes') or 0)>=float(task['totalTime'])
    except (ValueError,TypeError):return False


def subtask_reason(task):
    rules=policy()
    if not rules or ready(task):return ''
    name=str(task.get('name') or task.get('taskName') or '')
    if name in rules.get('task_names',{}):return rules['task_names'][name]
    if rules.get('remove_native_subtasks') and re.search(r'搜索|观看.{0,8}视频|顶部导航|浏览器通知|浏览器推送|(?:阅读|看).{0,8}小说|预约.*游戏|玩.*小游戏|体验\d*款游戏|收藏.*听书',name):
        return '当前缺少本人设备的实际操作，已暂时移除'
    if rules.get('remove_manual_subtasks') and re.search(r'贷款|借款|购买|购机|消费|支付|充值|订单|评价|评论|分享|发帖|加精|邀请|好友|导购|到店|核销|绑卡|实名认证|身份认证|会员等级升至|安装|体验应用|聊天|佩戴|群聊|消息回复',name):
        return '需要真实交易、本人内容、线下或设备操作，已暂时移除'
    return ''


def filter_catalog(value):
    if isinstance(value,list):
        return [filter_catalog(item) for item in value if not isinstance(item,dict) or not any(k in item for k in ('id','taskId')) or not subtask_reason(item)]
    if isinstance(value,dict):return {key:filter_catalog(item) if isinstance(item,(list,dict)) else item for key,item in value.items()}
    return value


def sign_reason(row):
    biz=str(row.get('bizCode') or '')
    return task_reason('sign_wallet' if biz in ('wallet','walletWelfare') else 'sign_'+biz,'sign.biz')


def filter_response(path,value):
    if not policy():return value
    if path.endswith('/signBizList.do') and isinstance(value,dict) and isinstance(value.get('data'),list):
        return {**value,'data':[row for row in value['data'] if not isinstance(row,dict) or not sign_reason(row)]}
    if path in ('/api/task/list.do','/api/task/listByType.do','/api/task/ec/taskList.do','/wukongapc/task/list'):
        return filter_catalog(value)
    return value


def apply_client(client):
    if getattr(client,'_temporary_policy_applied',False):return client
    original=getattr(client,'_call',None)
    if not callable(original):return client
    def call(host,path,*args,**kwargs):
        return filter_response(path,original(host,path,*args,**kwargs))
    client._call=call;client._temporary_policy_applied=True
    return client


def structurally_unavailable(entry):
    if entry.get('status') not in ('blocked','manual','action_required','failed'):return False
    detail=entry.get('details') if isinstance(entry.get('details'),dict) else {}
    kind=entry.get('reason_kind') or detail.get('reason_kind')
    if kind in ('manual','consumption','native_context','missing_context','session_unavailable','verification','unavailable','provider_unavailable','settlement','zero_reward'):return True
    message=str(entry.get('reason') or entry.get('message') or '')
    pattern=r'滑块|验证码|需本人|需要本人|原生会话|原生设备|导购|到店|核销|未发布|未开放|资格|邀请|需要.*积分|包含消耗|参数错误|活动无效|活动不存在|需要.*会员|SIM.*验证|真实地区|账号标签'
    if entry.get('status')!='failed':pattern+=r'|待适配|未匹配|已下线|已停用|需先完成|尚未完成'
    return bool(re.search(pattern,message))


def _unavailable(entry):
    return entry.get('status') in ('blocked','manual','action_required','failed') and (entry.get('name') in policy().get('task_names',{}) or structurally_unavailable(entry))


def prune_result(value):
    """Keep audit counts for removed prerequisites without turning them into successes."""
    if not policy().get('remove_manual_subtasks'):return value
    if isinstance(value,list):return [prune_result(item) for item in value]
    if not isinstance(value,dict):return value
    result=copy.deepcopy(value)
    rows=result.get('results')
    if isinstance(rows,list):
        previous_descendants=sum(row.get('excluded_count',0) for row in rows if isinstance(row,dict))
        excluded=[row for row in rows if isinstance(row,dict) and _unavailable(row)]
        result['results']=[prune_result(row) for row in rows if row not in excluded]
        descendants=sum(row.get('excluded_count',0) for row in result['results'] if isinstance(row,dict))
        if excluded or descendants:
            result['excluded_count']=(result['excluded_count']+len(excluded)+max(0,descendants-previous_descendants)) if 'excluded_count' in result else len(excluded)+descendants
            result['excluded_tasks']=[{'name':r.get('name','任务'),'reason':r.get('reason') or r.get('message') or '前置条件暂不可完成'} for r in excluded]
            if not result['results']:
                result.update(status='skipped',skip='当前不可执行分项已暂时移除')
            elif all(isinstance(row,dict) and row.get('status') in ('completed','success','skipped') for row in result['results']):
                result['status']='completed' if any(row.get('status') in ('completed','success') for row in result['results']) else 'skipped'
    return result


def account_reason(mode,account,raw):
    if not policy().get('skip_unavailable_accounts'):return ''
    from execution_policy import verification_hold
    if verification_hold(mode,account['id'],raw):return '账号需本人验证，自动任务已暂时移除'
    if raw.get('profile_kind')=='app':return '缺少本人小程序登录态，积分任务已暂时移除'
    if account.get('panel_status')=='offline' and (not raw.get('vivotoken') or account.get('status') in ('expired','pending_login')):
        return '账号在来源面板离线，任务已暂时移除'
    return ''
