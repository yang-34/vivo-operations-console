"""HAR-confirmed shop actions and additional active vivo campaign entries."""
import json,re
from pathlib import Path
from urllib.parse import quote,urlparse
from .taskflow.protocol_common import CURRENT,ActionRequired,ProtocolError
from .taskflow.added_common import Journal
from .taskflow.vivo import VivoClient
from .taskflow.vivo_native_earn import Native,SHOP
from store import today
from .task_policy import policy,subtask_reason

SITES=('wk2609303c903cd9','wk2604101581e0d4','wk260923cff5d7dd','wk260923db5f0ce9','wk230724d2178070')
BENEFITS=(231,521)
COUPONS=(383,1458,1459,1460,1464,1465,1467)

def campaigns(api):
    from .activities import execute_dynamic
    return execute_dynamic(api,only_sites=SITES)

def membership(api):
    """Current free benefits/coupons; every captured ID is re-queried for this account."""
    from .taskflow.vivo_native_earn import MSHOP
    context=api.source_context;token=CURRENT.set(context);entries=[]
    try:
        client=VivoClient(context.credentials,context.ensure_allowed);native=Native(client)
        native.shop_session();journal=Journal(context,'har_membership')
        member=native.call(MSHOP,'/member/query') or {}
        level=int(member.get('level') or 1)
        for kind,ids in [('benefit',BENEFITS),('coupon',COUPONS)]:
            path='/memberBenefit/queryActivity' if kind=='benefit' else '/coupon/v2/queryCouponActivity'
            for activity in ids:
                def read():return native.call(MSHOP,path,params={'activityId':activity}) if kind=='benefit' else native.call(MSHOP,path,method='POST',body={'activityId':activity,'defaultStyle':1,'needStringify':False})
                try:data=read() or {}
                except (ActionRequired,ProtocolError) as error:
                    entries.append({'name':f'{kind}:{activity}','status':'skipped','reason':getattr(error,'message',str(error))});continue
                items=data.get('benefitInfoDTOList' if kind=='benefit' else 'couponModuleItems') or []
                for item in items:
                    name=item.get('name') or item.get('couponName') or f'{kind}:{activity}'
                    if subtask_reason({'name':name,'id':item.get('memberBenefitId') or item.get('couponUuid')}):continue
                    if any(float(item.get(k) or 0)>0 for k in ('pointsCost','cost','price','deductPoints')):
                        entries.append({'name':name,'status':'skipped','reason':'需要消耗积分或付费，默认关闭'});continue
                    if kind=='benefit':
                        if item.get('isReceived')==1 or not item.get('hasStock') or item.get('isExpire'):
                            entries.append({'name':name,'status':'skipped','reason':'已领取、无库存或已过期'});continue
                        if level<int(item.get('requiredLevel') or 1) or item.get('checkSim'):
                            entries.append({'name':name,'status':'blocked','reason':'需要对应会员等级或本人 SIM 验证'});continue
                        ident=item.get('memberBenefitId');body={'from':'','activityId':activity,'benefitId':ident};send='/memberBenefit/receive'
                    else:
                        if item.get('couponFlag')==1 or item.get('receiveStatus') not in (211200,211201,211215):
                            entries.append({'name':name,'status':'skipped','reason':'已领取、未开放、已结束或当前等级不符合'});continue
                        if item.get('userAddressLimit') or item.get('userTagLimit'):
                            entries.append({'name':name,'status':'blocked','reason':'需要真实地区或账号标签资格'});continue
                        ident=item.get('couponUuid');body={'activityId':activity,'activityUuid':(data.get('couponModule') or {}).get('activityUuid',''),'couponUuid':ident,'couponType':item.get('couponType',2)};send='/coupon/receive/v2'
                    if not ident:continue
                    key=f'{today()}:{kind}:{activity}:{ident}'
                    saved=journal.saved(key)
                    if saved.get('state')=='confirmed':
                        entries.append({'name':name,'status':'blocked','reason':'此前已确认领取，当前列表尚未同步；本次不重复提交'});continue
                    if saved.get('state')=='rejected' and (str(saved.get('provider_code')) in ('800102','21057','1050','1018') or saved.get('parameter_forms_checked')):
                        if policy().get('remove_manual_subtasks'):continue
                        entries.append({'name':name,'status':'blocked','reason':saved.get('provider_message') or '此前已明确拒绝，未重复提交','provider_code':saved.get('provider_code')});continue
                    form_attempted=False
                    try:
                        def submit():
                            nonlocal form_attempted
                            try:return native.call(MSHOP,send,method='POST',body=body,writes=True)
                            except ActionRequired as error:
                                # Some activity controllers bind only form parameters.
                                # Retry only an explicit rejection, never a timeout or verification.
                                code=str(error.diagnostics.get('provider_code'))
                                rejected_activity=code in ('500','211415') and any(word in error.message for word in ('活动不存在','活动无效'))
                                if not rejected_activity:raise
                                form_attempted=True
                                value=native.call(MSHOP,send,method='POST',form=body,writes=True)
                                for issue in getattr(context.transport,'observations',[]):
                                    if issue.get('endpoint')==send and str(issue.get('code')) in ('500','211415'):
                                        issue.update(status='skipped',resolved_by='form_parameters')
                                return value
                        journal.submit(key,submit)
                        after=read() or {};rows=after.get('benefitInfoDTOList' if kind=='benefit' else 'couponModuleItems') or []
                        match=next((x for x in rows if x.get('memberBenefitId' if kind=='benefit' else 'couponUuid')==ident),{})
                        done=match.get('isReceived')==1 if kind=='benefit' else match.get('couponFlag')==1 or match.get('receiveStatus') in (211203,211208)
                        if done:journal.confirm(key)
                        entries.append({'name':name,'status':'completed' if done else 'blocked','reason':'领取状态已回读确认' if done else '已提交但领取状态未更新，保留记录不重发'})
                    except (ActionRequired,ProtocolError) as error:
                        if form_attempted and journal.saved(key).get('state')=='rejected':journal.put(key,{**journal.saved(key),'parameter_forms_checked':True})
                        entries.append({'name':name,'status':'blocked','reason':getattr(error,'message',str(error))})
        return {'results':entries} if entries else {'skip':'当前未下发符合条件的免费礼包或优惠券'}
    except (ActionRequired,ProtocolError) as error:return {'status':'partial','reason':getattr(error,'message',str(error))}
    finally:CURRENT.reset(token)

def shop_action(api,kind):
    context=api.source_context;token=CURRENT.set(context)
    try:
        client=VivoClient(context.credentials,context.ensure_allowed);native=Native(client)
        native.shop_session();journal=Journal(context,'har_shop_actions');key=today()+':'+kind
        if journal.saved(key).get('state') in ('pending','confirmed'):
            return {'status':'blocked','reason':'本动作已提交；只回查平台任务状态，不重复操作'}
        if kind=='shop_member':
            client.visit('https://member.vivo.com.cn/wap/index');journal.confirm(key)
            return {'status':'completed','reason':'已访问会员中心，随后以任务列表核验进度'}
        home=native.eden('/v3/home/index');text=json.dumps(home)
        pairs=list(dict.fromkeys(re.findall(r'/wap/product/(\d+)\?skuId=(\d+)',text)))
        if not pairs:return {'status':'blocked','reason':'当前官方商品目录未提供可核验的商品规格'}
        spu,sku=pairs[0]
        if kind=='shop_detail':
            client.visit(f'https://shop.vivo.com.cn/wap/product/{spu}?skuId={sku}')
            event=api._sdk_upload('com.vivo.space','2099354767390621',1)
            notice=(event.get('data') or {}).get('sdkTaskNotifyVo') or {}
            if notice:api._sdk_receive('com.vivo.space',notice.get('actionId') or '2099354767390621')
            journal.confirm(key)
            return {'status':'completed','reason':'已访问真实商品详情并按已核验 SDK 事件回读奖励'}
        if kind=='shop_search':
            client.visit('https://shop.vivo.com.cn/wap/search?keyword='+quote('vivo'))
            journal.confirm(key)
            return {'status':'completed','reason':'已访问商城搜索页面，随后以任务列表核验进度'}
        if kind=='shop_cart':
            csrf=native.call(SHOP,'/wap/fbApi/v1/my/token')
            before=native.call(SHOP,'/wap/fbApi/v2/shopcart/query/list',params={'token':csrf})
            if re.search(r'"skuId"\s*:\s*"?'+re.escape(sku)+r'"?(?=\s*[,}])',json.dumps(before)):
                journal.confirm(key);return {'status':'completed','reason':'选定商品已在购物车，未重复增加数量'}
            result=journal.submit(key,lambda:native.call(SHOP,'/wap/fbApi/v2/shopcart/addCommodity',method='POST',params={'token':csrf},body={'num':1,'mainSku':{'skuId':sku},'serviceSkuList':[],'bundleSkuList':[],'additionSkuList':[]},writes=True))
            after=native.call(SHOP,'/wap/fbApi/v2/shopcart/query/list',params={'token':csrf})
            if not re.search(r'"skuId"\s*:\s*"?'+re.escape(sku)+r'"?(?=\s*[,}])',json.dumps(after)):
                return {'status':'blocked','reason':'加购响应已收到，购物车尚未确认；保留记录不重发'}
            journal.confirm(key);return {'status':'completed','reason':'已添加一件当前商品并回查购物车；没有提交购买订单'}
        raise ValueError('未知商城动作')
    except (ActionRequired,ProtocolError) as error:return {'status':'blocked','reason':getattr(error,'message',str(error))}
    finally:CURRENT.reset(token)
