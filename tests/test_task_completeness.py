import json
from urllib.parse import quote
import requests
from protocol.api import Account,VivoApi
from protocol.runner import make_api,classify,safe_log
from protocol.transport import Transport
from protocol.tasks import execute_center,execute_sdk_package,route_task,perform_task
from protocol.activities import configured_activities


def test_reused_session_installs_account_cookies_for_all_business_hosts():
    transport=Transport()
    transport.reset_account();make_api({'openid':'first-account','vivotoken':'first-token'},transport)
    for host in ('pointh5','wukongapi','pointactivity','bbs','member','shop'):
        cookie=transport.prepare_request(requests.Request('GET',f'https://{host}.vivo.com.cn/')).headers['Cookie']
        assert 'vvc_r=first-token' in cookie and 'vivo_account_cookie_iqoo_openid=first-account' in cookie
    transport.reset_account();make_api({'openid':'next-account','vivotoken':'next-token'},transport)
    cookie=transport.prepare_request(requests.Request('GET','https://bbs.vivo.com.cn/')).headers['Cookie']
    assert 'first-token' not in cookie and 'next-token' in cookie


def test_completed_task_with_unclaimed_points_is_claimed_and_verified():
    class Api:
        def __init__(self):self.points=5;self.claimed=0
        def h5_call(self,*args,**kwargs):return {'retcode':0,'data':{'tasks':[{'list':[{'id':1,'name':'待领积分','taskComplete':True,'unreceviedPoints':self.points}]}]}}
        def sign_biz_names(self):return {}
        def _tasks_for_pkg(self,*args):raise AssertionError('Per-task reload is not needed')
        def complete_task(self,*args):self.claimed+=1;self.points=0;return {}
    api=Api();result=execute_center(api,['com.vivo.space'])
    assert api.claimed==1 and result['completed']==1 and result['already']==0


def test_sdk_upload_success_is_insufficient_to_mark_reward_complete():
    class Api:
        ok=staticmethod(VivoApi.ok)
        def h5_call(self,*args,**kwargs):return {'code':0,'data':{'actions':[{'actionId':'one'},{'actionId':'two'}]}}
        def _sdk_upload(self,*args):return {'retcode':0,'data':{'taskId':'pending-task'}}
        def _sdk_receive(self,pkg,aid):return {'retcode':0 if aid=='one' else 999,'message':'未满足条件'}
    saved=[];result=execute_sdk_package(Api(),'com.vivo.space',save=saved.append)
    assert result['uploaded']==2 and result['points_claimed']==1 and saved==['sdk:com.vivo.space:one']
    assert classify(result)=='partial' and result['results'][1]['receive']['code']==999


def test_consecutive_rewards_use_only_server_supplied_order_numbers():
    api=VivoApi(Account({'openid':'test','vivotoken':'test'}));calls=[]
    def call(host,path,**kwargs):
        calls.append((path,kwargs))
        return {'code':0,'data':{'signRewardInfos':[{'signAwardOrderNo':'server-order'}]}} if path.endswith('info.do') else {'code':0}
    api.h5_call=call
    assert classify(api.consecutive_sign_receive())=='completed'
    assert next(kwargs for path,kwargs in calls if path.endswith('/receive.do'))['params']['signAwardOrderNo']=='server-order'
    calls.clear();api.h5_call=lambda *a,**k:{'code':0,'data':{'signRewardInfos':[]}}
    assert api.consecutive_sign_receive()['skip']


def test_manual_requirements_and_missing_real_ad_parameters_are_explicit():
    assert route_task({'name':'官网购物送积分'})[0]=='manual'
    assert route_task({'name':'会员等级升至白银会员'})[0]=='eligibility'
    assert route_task({'name':'浏览网页','webTask':'1'})[0]=='web'
    result=perform_task(object(),{'id':9,'type':'9','name':'广告任务'},{})
    assert result['status']=='manual' and 'traceId' in result['reason']


def test_activity_configuration_preserves_all_component_ids_and_families():
    value={'config':quote(json.dumps({'signin':{'activityId':'123'},'lottery':{'activityId':'456'},'task':{'activityId':'789'}}))}
    activities=configured_activities(value)
    assert set(activities)=={'123','456','789'}
    assert 'signin' in activities['123'] and 'lottery' in activities['456']


def test_full_details_preserve_every_result_and_redact_nested_credentials():
    value=safe_log({'results':[{'id':i,'token':'secret','_err':'url?token=secret'} for i in range(80)]})
    assert len(value['results'])==80 and 'secret' not in json.dumps(value)


def test_ec_progress_is_verified_without_overriding_explicit_server_false():
    from protocol.tasks import is_complete
    assert is_complete({'completedTimes':1,'totalTime':1})
    assert not is_complete({'completedTimes':1,'totalTime':1,'taskComplete':False})
    assert not is_complete({'completedTimes':0,'totalTime':0})


def test_real_plugin_shapes_route_to_distinct_activity_families():
    value={'pages':[{'items':[
        {'pluginInfo':{'enName':'wukong-sign-front'},'pluginConfig':{'activityInfo':{'activityId':'sign-id','activityType':13}}},
        {'pluginInfo':{'enName':'wukong-luckynumber-offline'},'pluginConfig':{'activityInfo':{'activityId':'lucky-id'}}},
        {'pluginInfo':{'enName':'wukong-squaredpaper'},'pluginConfig':{'activityInfo':{'activityId':'draw-id','activityType':5}}},
    ]}]}
    result=configured_activities(value)
    assert result['sign-id']=={'signin'} and result['lucky-id']=={'lucky'} and result['draw-id']=={'lottery'}


def test_step_api_returns_structured_complete_details(client):
    from store import db,now
    with db('demo') as c:
        c.execute("INSERT INTO batches VALUES('detail','detail','full','completed',1,1,?,?)",(now(),now()))
        aid=c.execute('SELECT id FROM accounts LIMIT 1').fetchone()[0]
        rid=c.execute("INSERT INTO runs(batch_id,account_id) VALUES('detail',?)",(aid,)).lastrowid
        c.execute('INSERT INTO steps(run_id,label,status,created_at,details) VALUES(?,?,?,?,?)',(rid,'SDK','partial',now(),json.dumps({'result':{'results':[{'id':i} for i in range(80)]}})))
    response=client.get(f'/api/runs/{rid}/steps').json
    assert len(response['items'][0]['details']['result']['results'])==80


def test_progressing_task_continues_until_server_target_instead_of_twelve_round_cap():
    class Api:
        def __init__(self):self.progress=0
        def h5_call(self,*args,**kwargs):return {'retcode':0,'data':{'tasks':[{'hasMore':'false','list':[{'id':1,'name':'分段网页任务','taskComplete':self.progress>=20,'completedTimes':self.progress,'totalTime':20}]}]}}
        def sign_biz_names(self):return {}
        def _tasks_for_pkg(self,*args):raise AssertionError('No blind reload')
        def complete_task(self,*args):self.progress+=1;return {}
    api=Api();result=execute_center(api,['com.vivo.space'])
    assert api.progress==20 and result['completed']==1


def test_sdk_receipt_uses_notification_action_and_records_all_rows():
    class Api:
        ok=staticmethod(VivoApi.ok)
        def h5_call(self,*args,**kwargs):return {'code':0,'data':{'actions':[{'actionId':'reported'}]}}
        def _sdk_upload(self,*args):return {'code':0,'data':{'taskId':'task-id','sdkTaskNotifyVo':{'actionId':'receipt-action','notifyContent':'<b>访问会员中心</b>','delay':0}}}
        def _sdk_receive(self,pkg,aid):
            assert aid=='receipt-action';return {'code':0}
    result=execute_sdk_package(Api(),'com.vivo.space')
    assert len(result['results'])==1 and result['results'][0]['name']=='访问会员中心' and result['results'][0]['task_id']=='task-id'
