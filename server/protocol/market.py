"""Live product queries and explicit exchange contracts. Read paths never submit an order."""
import json
from decimal import Decimal,InvalidOperation
from urllib.parse import urlparse
from pathlib import Path
from .common import POINTH5,POINTWAP,now_ms

PROTOCOL_VERSION='20261013_v6.1.7'
POINTSTORE='https://pointstore.vivo.com.cn'

def mall_host(metadata):
    return POINTWAP if metadata.get('city') not in (None,'','全国') or str(metadata.get('categoryId'))=='47' else POINTH5

def mall_call(api,metadata,path,params=None,method='GET'):
    host=mall_host(metadata)
    official=path in ('/api/mall/item/detail.do','/api/mall/item/exchange.do','/api/sms/send.do','/api/sms/verify.do') and metadata.get('redemption_kind')!='store' and str(metadata.get('categoryId'))!='47'
    if path=='/api/order/detail.do':host=POINTH5
    elif official:host=POINTWAP # HAR page URL is pointstore; HTTP/2 :authority is pointwap.
    headers=api._h5_headers('com.vivo.space');query={'pts_sys_ver':PROTOCOL_VERSION,'pts_sys_t':now_ms()}
    if official and getattr(api,'app_session',None):
        headers.update({'Origin':POINTSTORE,'Referer':POINTSTORE+'/','X-Requested-With':'com.vivo.space'})
    elif official:
        from .devices import webview_ua,resolve_device
        device=getattr(getattr(api,'acct',None),'device',None) or resolve_device({})
        headers.update({'Origin':POINTSTORE,'Referer':POINTSTORE+'/','X-Requested-With':'com.vivo.space',
          'User-Agent':'VivoSpace/2.1 (Android)_(vivo.com)/13.5.0.5_13505 web DeviceType/phone '+webview_ua(device)})
        cookie=next((key for key in headers if key.lower()=='cookie'),'Cookie')
        extra=f"vvc_app_version=13505;vvc_app_name=13.5.0.5;vivo_member_source_type=APP_H5;isvivophone=1;vvc_model={device['model']};vvc_av={device['sdk_int']};vvc_an={device['android_ver']}"
        headers[cookie]=(headers.get(cookie,'')+';'+extra).lstrip(';')
    values=dict(params or {})
    if method=='GET':query.update(values);extra={}
    else:headers['Content-Type']='application/json';extra={'json':values}
    response=api.s.request(method,host+path,params=query,headers=headers,timeout=25,**extra)
    value=response.json()
    if not isinstance(value,dict):raise ValueError('商城未返回有效业务响应')
    return value


def checked(response):
    if not isinstance(response,dict):raise ValueError('商品接口未返回有效数据')
    if str(response.get('retcode',response.get('code'))) not in ('0','200'):
        raise ValueError('商品接口查询失败：'+str(response.get('message') or response.get('msg') or response.get('retcode','未知状态'))[:160])
    return response.get('data') or {}


def categories(api):
    output={}
    for channel,path in [('point','/api/mall/v5/home/categories.do'),('EC','/api/mall/v5/ec/categories.do')]:
        data=checked(api._pm_get(path))
        for item in data.get('categories') or []:
            output[(channel,str(item['id']))]={**item,'channel':item.get('channel') or channel}
    return list(output.values())


def scopes(api,scope,selected=None):
    result=[]
    if scope in ('all','national'):
        for category in categories(api):
            if str(category['id'])=='47':continue
            result.append({'categoryId':category['id'],'city':'全国','channel':category['channel']})
    if scope in ('all','cities'):
        cities=json.loads(Path(__file__).with_name('cities.json').read_text(encoding='utf-8'))
        for name,cid in cities.items():
            if selected and name not in selected:continue
            for kind in (1,3):result.append({'categoryId':47,'city':name,'vkCityId':cid,'itemType':kind,'channel':'point'})
    return result


def scope_key(spec):
    return ':'.join(str(spec.get(k,'')) for k in ('channel','categoryId','city','itemType'))


def normalize(item,spec):
    if str(item.get('playMethod'))!='0':return None
    iid=item.get('id',item.get('itemId'))
    price=next((item[k] for k in ('finalPoints','originPoints','price') if item.get(k) is not None),None)
    if iid is None or price is None:return None
    try:
        price=Decimal(str(price))
        if not price.is_finite() or price<0 or price!=price.to_integral_value():return None
    except InvalidOperation:return None
    stock=item.get('availStock')
    if stock is None:stock=int(item.get('store') or 0)-int(item.get('storeUse') or 0)
    city=spec.get('city') or '全国';channel=str(spec.get('channel') or 'point')
    metadata={**spec,'itemId':iid,'channel':channel,'source':4 if channel.lower()=='ec' else 0,'playMethod':0}
    for field in ('providerType','type','storeVerifyWay'):
        if item.get(field) is not None:metadata[field]=item[field]
    image=str(item.get('image') or '')
    if image.startswith('//'):image='https:'+image
    if urlparse(image).scheme not in ('https','http'):image=''
    return {'id':f'{city}:{iid}' if channel.lower()=='point' else f'{city}:EC:{iid}',
      'name':str(item.get('name') or '未命名商品'),'price':int(price),'stock':max(0,int(stock or 0)),
      'image':image,'category':'生活礼券' if str(item.get('itemType',spec.get('itemType')))=='1' else '生活好物',
      'description':f'{city} · 实时查询，兑换以平台最终确认结果为准','metadata':metadata}


def read_scope(api,spec):
    seen=set();output=[];page=1;cursor=None
    while True:
        params={'categoryId':spec['categoryId'],'pageNo':page}
        if cursor:params['pageContext']=cursor
        if spec.get('city')!='全国':params.update(city=spec['city'].replace('市',''),vkCityId=spec.get('vkCityId'),itemType=spec.get('itemType'))
        endpoint='/api/mall/feed/items.do' if spec.get('city')=='全国' else '/api/mall/v5/home/items.do'
        data=checked(api.h5_call(mall_host(spec),endpoint,params={**params,'pts_sys_ver':PROTOCOL_VERSION},pkg='com.vivo.space'))
        items=data.get('items')
        if not isinstance(items,list):raise ValueError('平台未返回商品列表')
        fresh=[item for item in items if str(item.get('id',item.get('itemId'))) not in seen]
        if items and not fresh:raise ValueError('平台重复返回同一页，已停止该范围更新')
        for item in fresh:
            seen.add(str(item.get('id',item.get('itemId'))));product=normalize(item,spec)
            if product:output.append(product)
        cursor=data.get('pageContext')
        if not items:break
        if 'hasNext' in data:
            if str(data['hasNext']).lower() not in ('true','1'):break
        elif cursor:
            import base64
            try:finished=json.loads(base64.b64decode(cursor)).get('lastPageFlag') is True
            except (ValueError,TypeError):finished=True
            if finished:break
        else:break
        page+=1
    return output


def current_offer(api,metadata):
    spec={**metadata,'city':metadata.get('city') or '全国','channel':metadata.get('channel') or 'point'}
    specs=[spec]
    if spec['city']!='全国' and not spec.get('itemType'):specs=[{**spec,'itemType':1},{**spec,'itemType':3}]
    for target in specs:
        for offer in read_scope(api,target):
            if str(offer['metadata']['itemId'])==str(metadata['itemId']):
                detail_params={key:target[key] for key in ('city','channel','categoryId','vkCityId','itemType') if target.get(key) is not None}
                if detail_params.get('city')!='全国':detail_params['city']=str(detail_params.get('city') or '').removesuffix('市')
                detail=checked(mall_call(api,target,'/api/mall/item/detail.do',{'id':metadata['itemId'],**detail_params}))
                item=detail.get('item') or detail
                if not isinstance(item,dict) or str(item.get('id'))!=str(metadata['itemId']):raise ValueError('平台未返回匹配的商品详情')
                if str(item.get('playMethod'))!='0':raise ValueError('该商品当前需要抽奖或现金支付，不能作为积分兑换提交')
                if item.get('finalPrice') is not None:
                    value=Decimal(str(item['finalPrice']))
                    if not value.is_finite() or value<0 or value>10**12 or value!=value.to_integral_value():raise ValueError('商品详情价格无效')
                    offer['price']=int(value)
                offer['metadata'].update({key:item[key] for key in ('providerType','type','storeVerifyWay','name','image','finalPrice') if key in item})
                offer['metadata']['redemption_kind']='store' if str(item.get('providerType'))=='2' else 'cloud' if str(item.get('type'))=='20' else 'standard'
                return offer
    raise ValueError('商品已下架或当前账号不可见，请更新商品后重选')


def stores(api,metadata):
    result=[];seen=set();page=1
    while True:
        data=checked(mall_call(api,metadata,'/api/store/list.do',dict(skuCode=metadata['itemId'],page=page,city=metadata.get('city','').replace('全国',''),vkCityId=metadata.get('vkCityId') or '',itemType=metadata.get('type') or metadata.get('itemType') or '',sort='s',district='')))
        rows=data.get('store') or []
        if not isinstance(rows,list):raise ValueError('平台门店数据无效')
        added=0
        for row in rows:
            code=str(row.get('storeCode') or row.get('code') or '')
            if not code or code in seen:continue
            seen.add(code);added+=1
            if row.get('itemAvail') is True or str(row.get('itemAvail'))=='1':result.append({'code':code,'name':str(row.get('storeName') or row.get('name') or code),'address':str(row.get('address') or '')})
        if not added or str(data.get('hasNext')).lower() not in ('true','1'):break
        page+=1
    return result


def order_details(api,metadata,order_id):
    value=checked(mall_call(api,{'city':'全国'},'/api/order/detail.do',{'orderId':order_id,'netType':'wifi','itemType':metadata.get('type') or metadata.get('itemType') or 1}))
    order=value.get('order') or {}
    if str(order.get('id'))!=str(order_id):raise ValueError('订单详情未返回匹配的订单号')
    return value


def fulfillment_from_order(detail):
    order=detail.get('order') or {};item=detail.get('item') or {}
    result={key:order[key] for key in ('couponCode','cardNo','cardPwd','redeemCode','activationCode','deliveryInfo','couponExpirationTime','expireTime','receiveUrl') if order.get(key)}
    if item.get('decs'):result['instructions']=item['decs']
    elif item.get('description'):result['instructions']=item['description']
    return result
