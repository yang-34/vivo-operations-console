import base64,json
from types import SimpleNamespace
from protocol.devices import resolve_device
from protocol.market import mall_host,mall_call,read_scope
from protocol.tasks import route_task

def test_vivo_profile_does_not_copy_non_vivo_model_or_shared_har_identifiers():
    device=resolve_device({'device':{'model':'23049RAD8C','brand':'Xiaomi','imei':'owned-id'}})
    assert device['brand']=='vivo' and device['model']=='V2227A'
    assert device['imei']=='owned-id' and not device['native_verified']
    original=resolve_device({'device':{'model':'V2227A','brand':'vivo','sdk_int':33,'android_ver':'13'}})
    assert original['native_verified']

def test_city_exchange_uses_har_host_json_and_version():
    seen=[]
    class Session:
        def request(self,method,url,**kw):seen.append((method,url,kw));return SimpleNamespace(json=lambda:{'retcode':0})
    api=SimpleNamespace(s=Session(),_h5_headers=lambda pkg:{})
    payload={'categoryId':'47','item':{'itemCode':1},'store':{'storeCode':'store'},'offlineActivityKey':'','channel':'point','source':0}
    mall_call(api,{'city':'昆明','categoryId':47},'/api/mall/order/vk/doExchange.do',payload,'POST')
    method,url,kw=seen[0]
    assert url=='https://pointwap.vivo.com.cn/api/mall/order/vk/doExchange.do'
    assert kw['json']==payload and 'jvq_param' not in kw['json']
    assert kw['params']['pts_sys_ver']=='20261013_v6.1.7'

def test_feed_cursor_fetches_following_pages():
    pages=[]
    class Api:
        def h5_call(self,host,path,**kwargs):
            assert path=='/api/mall/feed/items.do'
            params=kwargs['params'];pages.append(params)
            end=params['pageNo']==2
            cursor=base64.b64encode(json.dumps({'lastPageFlag':end}).encode()).decode()
            return {'retcode':0,'data':{'items':[{'id':params['pageNo'],'name':'item','playMethod':0,'finalPoints':5,'availStock':2}],'pageContext':cursor}}
    assert len(read_scope(Api(),{'categoryId':120,'city':'全国'}))==2
    assert 'pageContext' in pages[1]

def test_sdk_har_envelope_contains_vivo_profile_without_captured_account():
    from protocol.api import VivoApi,Account
    from protocol.taskflow.vendor.vivo import jvq
    captured=[]
    class Session:
        def post(self,url,**kw):
            captured.append(json.loads(jvq.decrypt_response(kw['json']['jvq_param'])))
            return SimpleNamespace(json=lambda:{'code':0,'data':{}})
    api=VivoApi(Account({'openid':'owned-account','vivotoken':'owned-token'}));api.s=Session()
    api._sdk_upload('com.vivo.space','2099354767390621',1)
    body=captured[0]
    assert body['model']=='V2227A' and body['openid']=='owned-account'
    assert body['sdkVerCode']=='1360' and body['notifyPattern']=='0' and body['count']=='1'

def test_shopping_and_manual_requirements_are_not_misrouted_to_wish():
    assert route_task({'name':'将商品加入购物车'})[0]=='shop_cart'
    assert route_task({'name':'浏览商品详情页'})[0]=='shop_detail'
    assert route_task({'name':'搜索1款商品'})[0]=='shop_search'
    assert route_task({'name':'访问会员中心'})[0]=='shop_member'
    assert route_task({'name':'学生身份认证'})[0]=='manual'

def test_native_sdk_only_claims_server_notified_rewards():
    from protocol.tasks import execute_sdk_package
    from protocol.api import VivoApi
    class Api:
        ok=staticmethod(VivoApi.ok)
        _native_sdk=True
        def h5_call(self,*a,**kw):return {'code':0,'data':{'actions':[{'actionId':'a'},{'actionId':'b'}]}}
        def _sdk_upload(self,pkg,aid,count):return {'code':0,'data':{'sdkTaskNotifyVo':{'actionId':'a','delay':0}}} if aid=='a' else {'code':0,'data':{'taskId':'non-reward-event'}}
        def _sdk_receive(self,pkg,aid):assert aid=='a';return {'code':0}
    result=execute_sdk_package(Api(),'com.vivo.space')
    assert result['points_claimed']==1 and result['not_applicable']==1

def test_city_product_preview_uses_har_city_and_item_type(monkeypatch):
    import protocol.market as market
    calls=[]
    monkeypatch.setattr(market,'read_scope',lambda *a:[{'price':7,'stock':10,'metadata':{'itemId':20037975}}])
    def query(api,metadata,path,params):
        calls.append((market.mall_host(metadata),params))
        return {'retcode':0,'data':{'item':{'id':20037975,'playMethod':0,'finalPrice':5,'providerType':2,'type':1,'name':'Gift'}}}
    monkeypatch.setattr(market,'mall_call',query)
    result=market.current_offer(object(),{'itemId':20037975,'categoryId':47,'city':'昆明','vkCityId':'201910150000001312','itemType':1})
    assert result['price']==5 and result['metadata']['redemption_kind']=='store'
    assert calls[0][0]=='https://pointwap.vivo.com.cn'
    assert calls[0][1]['city']=='昆明' and calls[0][1]['itemType']==1

def test_har_membership_never_claims_when_fee_or_level_is_required(client,monkeypatch):
    from protocol import har_tasks
    context=SimpleNamespace(mode='live',account_id=1,credentials={'scopes':{}},persist_scopes=lambda value:None,ensure_allowed=lambda:None)
    monkeypatch.setattr(har_tasks,'VivoClient',lambda *a:None)
    class Native:
        def __init__(self,*a):pass
        def shop_session(self):pass
        def call(self,host,path,**kw):
            if path=='/member/query':return {'level':1}
            if path=='/memberBenefit/queryActivity':return {'benefitInfoDTOList':[{'name':'Requires points','hasStock':1,'pointsCost':5},{'name':'Higher member','hasStock':1,'requiredLevel':2,'memberBenefitId':1}]}
            raise AssertionError('Must not submit a paid or ineligible claim')
    monkeypatch.setattr(har_tasks,'Native',Native);monkeypatch.setattr(har_tasks,'BENEFITS',(231,));monkeypatch.setattr(har_tasks,'COUPONS',())
    result=har_tasks.membership(SimpleNamespace(source_context=context))
    assert [r['status'] for r in result['results']]==['skipped','blocked']

def test_guess_reward_uses_current_endpoint_without_obsolete_activity_parameter(client):
    from protocol.taskflow.vivo_source_flows import guess_rewards
    class Client:
        http=SimpleNamespace(hosts=set(),writing=False)
        def _call(self,host,path,**kw):
            if path.endswith('explicit/list.do'):return {'data':{'guessActivity':{'id':123}}}
            assert path=='/api/activity/guess/v2/receive.do' and kw['params']=={}
            return {'data':{}}
    reads=[]
    def profile(c):reads.append(1);return SimpleNamespace(snapshot={'points':100 if len(reads)==1 else 110})
    ctx=SimpleNamespace(mode='live',account_id=1,credentials={'scopes':{}},persist_scopes=lambda s:None)
    result=guess_rewards(SimpleNamespace(profile=profile),Client(),ctx,{'points':100})
    assert result.status=='success'

def test_verification_reason_keeps_platform_message():
    import pytest
    from protocol.taskflow.added_common import checked
    from protocol.taskflow.protocol_common import ActionRequired
    with pytest.raises(ActionRequired) as failure:checked({'code':21057,'message':'请输入滑块验证码'},writing=True)
    assert failure.value.diagnostics['provider_message']=='请输入滑块验证码'
    assert failure.value.diagnostics['provider_result_status']=='action_required'

def test_national_accounts_still_query_all_ec_tasks():
    from protocol.api import Account,VivoApi
    queries=[]
    class Session:
        def get(self,url,**kw):queries.append(kw['params']);return SimpleNamespace(json=lambda:{'retcode':0,'data':{'tasks':[{'id':15,'name':'将商品加入购物车'}]}})
    api=VivoApi(Account({'openid':'own','vivotoken':'token','city':'全国'}));api.s=Session();api._gap=lambda:None
    assert api._ec_list('com.vivo.space')[0]['id']==15
    assert queries and 'city' not in queries[0] and 'vkCityId' not in queries[0]

def test_member_claim_form_fallback_only_follows_explicit_rejection(client,monkeypatch):
    from protocol import har_tasks
    from protocol.taskflow.protocol_common import ActionRequired
    methods=[];received=[False]
    ctx=SimpleNamespace(mode='live',account_id=1,credentials={'scopes':{}},persist_scopes=lambda s:None,ensure_allowed=lambda:None,transport=SimpleNamespace(observations=[]))
    monkeypatch.setattr(har_tasks,'VivoClient',lambda *args:None)
    class Native:
        def __init__(self,*args):pass
        def shop_session(self):pass
        def call(self,host,path,**kw):
            if path=='/member/query':return {'level':1}
            if path=='/memberBenefit/queryActivity':return {'benefitInfoDTOList':[{'memberBenefitId':1,'name':'free','hasStock':1,'isReceived':int(received[0])}]}
            if path=='/memberBenefit/receive':
                methods.append('form' if 'form' in kw else 'json')
                if 'form' not in kw:raise ActionRequired('礼包活动不存在',provider_code='500',reason_kind='provider_rejected')
                received[0]=True;return {}
            raise AssertionError(path)
    monkeypatch.setattr(har_tasks,'Native',Native);monkeypatch.setattr(har_tasks,'BENEFITS',(231,));monkeypatch.setattr(har_tasks,'COUPONS',())
    result=har_tasks.membership(SimpleNamespace(source_context=ctx))
    assert methods==['json','form'] and result['results'][0]['status']=='completed'

def test_source_continuation_restores_root_package(monkeypatch):
    from protocol import source_bridge
    from protocol.taskflow import vivo
    from protocol.taskflow.sdk import StepResult
    from protocol.taskflow.vendor.vivo.taskcenter import DEFAULT_PACKAGE
    def construct(*args):return SimpleNamespace(http=SimpleNamespace(writing=False),pkg=DEFAULT_PACKAGE)
    monkeypatch.setattr(vivo,'VivoClient',construct)
    count=[]
    def run(self,client,ctx,key,snapshot):
        assert client.pkg==DEFAULT_PACKAGE
        count.append(1);client.pkg='another-business'
        return StepResult('success','ok',{'step':len(count)},continue_after=0 if len(count)==1 else None)
    monkeypatch.setattr(vivo.LiveVivo,'run',run)
    ctx=SimpleNamespace(credentials={},ensure_allowed=lambda:None)
    api=SimpleNamespace(source_context=ctx,pm_points=lambda:100)
    assert source_bridge.run_source(api,'app_tasks')['status']=='completed'
    assert len(count)==2

def test_visiting_html_is_not_reported_as_broken_json():
    from protocol.taskflow.protocol_common import CURRENT,GuardedSession
    calls=[]
    ctx=SimpleNamespace(transport=SimpleNamespace(request=lambda *a,**kw:calls.append(kw)))
    token=CURRENT.set(ctx)
    try:
        session=GuardedSession({'shop.vivo.com.cn'})
        session.get('https://shop.vivo.com.cn/wap/index',review_redirect=True)
        assert calls[-1]['allow_non_json'] is True and calls[-1]['allow_redirects'] is False
    finally:CURRENT.reset(token)

def test_nonparticipant_does_not_submit_guess_reward():
    from protocol.taskflow.vivo_source_flows import guess_rewards
    class Client:
        http=SimpleNamespace(hosts=set())
        def _call(self,host,path,**kw):
            assert path.endswith('explicit/list.do')
            return {'data':{'guessActivity':{'id':1,'isChipIn':False}}}
    result=guess_rewards(None,Client(),None,{'points':50})
    assert result.status=='skipped'

def test_continued_task_does_not_hide_earlier_blocked_results():
    from protocol.runner import classify
    assert classify({'status':'skipped','results':[{'status':'completed'},{'status':'blocked','reason':'需要原生会话'}]})=='partial'
    assert classify({'status':'completed','results':[{'status':'failed'},{'status':'completed'}]})=='partial'
    assert classify({'status':'completed','packages':11,'discovered':2,'results':[{'status':'pending'},{'status':'manual'}]})=='completed'
