import { useEffect,useRef,useState } from 'react'
import { Alert,App,Button,Form,Input,Modal,Select,Space } from 'antd'
import { CheckCircleOutlined,ExclamationCircleOutlined,GiftOutlined } from '@ant-design/icons'
import { api,requestId,type Row } from './api'
import { useWorkspace } from './context'
import Fulfillment from './Fulfillment'

export default function RedemptionDialog({product,onClose,initialAccountId}:{product:Row|null;onClose:()=>void;initialAccountId?:number}){
  const {mode,reload,navigate}=useWorkspace();const {message}=App.useApp();const [aid,setAid]=useState<number|undefined>(initialAccountId);const [query,setQuery]=useState('');const [accounts,setAccounts]=useState<Row[]>([])
  const [offers,setOffers]=useState<Row[]>([]);const [offerId,setOfferId]=useState<string>();const [offersLoading,setOffersLoading]=useState(false);const [offerError,setOfferError]=useState('');const [offersReload,setOffersReload]=useState(0)
  const [busy,setBusy]=useState(false);const [preflight,setPreflight]=useState<Row|null>(null);const [sms,setSms]=useState('');const [smsRequired,setSmsRequired]=useState(false);const [sentUntil,setSentUntil]=useState(0);const [now,setNow]=useState(Date.now());const [result,setResult]=useState<Row|null>(null);const [notice,setNotice]=useState('');const [challenge,setChallenge]=useState<Row|null>(null);const [ticket,setTicket]=useState('');const [constId,setConstId]=useState('');const [stores,setStores]=useState<Row[]>([]);const [storeCode,setStoreCode]=useState('');const frame=useRef<HTMLIFrameElement>(null);const key=useRef(requestId())
  useEffect(()=>{let active=true;Promise.all([api(`/accounts?status=active&sort=points&page_size=50&q=${encodeURIComponent(query)}`,mode),initialAccountId?api(`/accounts/${initialAccountId}`,mode):Promise.resolve(null)]).then(([r,selected])=>{if(active)setAccounts(selected&&!r.items.some((a:Row)=>a.id===initialAccountId)?[selected.account,...r.items]:r.items)}).catch(()=>{});return()=>{active=false}},[mode,query,initialAccountId])
  useEffect(()=>{
    let active=true;setOffers([]);setOfferId(undefined);setOfferError('')
    if(!product){setOffersLoading(false);return}
    if(Number(product.variant_count||1)<=1){setOffers([{id:product.id,city:product.city||'全国',price:product.price,stock:product.stock}]);setOfferId(product.id);setOffersLoading(false);return}
    setOffersLoading(true)
    api(`/products/${encodeURIComponent(product.id)}/variants`,mode).then(value=>{
      if(!active)return
      setOffers(value.items)
      if(value.items.length===1)setOfferId(value.items[0].id)
      else if(initialAccountId&&value.items.some((offer:Row)=>offer.id===product.id))setOfferId(product.id)
      if(!value.items.length)setOfferError('暂无可兑换城市，请刷新商品目录。')
    }).catch(error=>{if(active)setOfferError(error.message)}).finally(()=>{if(active)setOffersLoading(false)})
    return()=>{active=false}
  },[product?.id,product?.variant_count,mode,initialAccountId,offersReload])
  useEffect(()=>{const timer=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(timer)},[])
  useEffect(()=>{if(!challenge)return;const receive=(event:MessageEvent)=>{if(event.source!==frame.current?.contentWindow||event.origin!=='null'||event.data?.nonce!==challenge.nonce||event.data?.type!=='vivo-result')return;if(typeof event.data.ticket==='string'&&typeof event.data.constId==='string'){setTicket(event.data.ticket);setConstId(event.data.constId);setNotice('官方安全验证已完成，请确认兑换。');setChallenge(null)}};addEventListener('message',receive);return()=>removeEventListener('message',receive)},[challenge])
  const offer=offers.find(value=>value.id===offerId)
  const needsCity=Boolean(product?.has_city_variants)||offers.length>1||offers.some(value=>value.city!=='全国')
  const price=Number(preflight?.price??offer?.price??product?.price??0);const number=(v:unknown)=>Number(v||0).toLocaleString('zh-CN')
  function resetVerification(){setPreflight(null);setNotice('');setSms('');setSmsRequired(false);setSentUntil(0);setTicket('');setConstId('');setChallenge(null);setStores([]);setStoreCode('');key.current=requestId()}
  function ready(){if(!offer){message.warning('请选择兑换城市');return false}if(!aid||!product){message.warning('请选择兑换账号');return false}return true}
  async function check(){if(!ready()||!offer)return;setBusy(true);try{const value=await api('/redemptions/preflight',mode,{method:'POST',body:JSON.stringify({account_id:aid,product_id:offer.id})});setPreflight(value);setStoreCode('');setStores(value.redemption_kind==='store'?(await api('/redemptions/stores',mode,{method:'POST',body:JSON.stringify({account_id:aid,preflight_id:value.id})})).items:[]);setNotice('所选城市的实时价格、库存与账号余额已核对。')}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  async function verify(action:string){if(!aid||!preflight)return;setBusy(true);try{const value=await api('/redemptions/verification',mode,{method:'POST',body:JSON.stringify({account_id:aid,preflight_id:preflight.id,action})});if(action==='sms'){setSentUntil(Date.now()+60000);message.success(value.message)}else setChallenge(value)}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  async function submit(){if(!ready()||!offer)return;if(mode==='live'&&!preflight){await check();return}if(preflight?.redemption_kind==='store'&&!storeCode){message.warning('请选择核销门店');return}setBusy(true);try{const value=await api('/redemptions',mode,{method:'POST',body:JSON.stringify({account_id:aid,product_id:offer.id,idempotency_key:key.current,preflight_id:preflight?.id,sms_code:sms,ticket,const_id:constId,store_code:storeCode})});reload();if(['sms_required','captcha_required'].includes(value.status)){setNotice(value.message||'平台要求完成验证');setSmsRequired(value.status==='sms_required');key.current=requestId();if(value.status==='captcha_required')await verify('captcha')}else setResult(value)}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  return <Modal className="redemption-modal" open={!!product} title={result?'兑换结果':'确认兑换'} width={560} onCancel={busy?undefined:onClose} closable={!busy} maskClosable={!busy} onOk={result?onClose:submit} confirmLoading={busy} okButtonProps={{disabled:!result&&(offersLoading||!offer||Number(offer.stock)<=0||!!offerError)}} okText={result?'完成':mode==='live'&&!preflight?'核对实时价格':`确认兑换 · ${number(price)} 积分`} cancelText="取消" cancelButtonProps={{style:result?{display:'none'}:{}}}>
    {result?<div className="redemption-result"><div className={result.status==='success'?'green':'orange-text'}>{result.status==='success'?<CheckCircleOutlined/>:<ExclamationCircleOutlined/>}</div><h2>{result.status==='success'?'兑换成功':result.status==='unknown'?'结果待核实':'兑换未完成'}</h2><p>{result.message}</p><Fulfillment value={result.delivery}/>{mode==='live'&&result.status==='success'&&<Button loading={busy} onClick={async()=>{setBusy(true);try{setResult(await api(`/redemptions/${result.id}/refresh`,mode,{method:'POST',body:'{}'}));reload()}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}}>刷新激活信息</Button>}<p>订单编号：{result.id}</p>{result.status==='unknown'&&<Alert type="warning" showIcon message="订单已保留，核实前请勿重复兑换。"/>}<Button onClick={()=>{onClose();navigate('redemptions')}}>查看兑换记录</Button></div>:<>
      <div className="checkout-item"><div className="checkout-image">{product?.image?<img alt={product.name} src={product.image}/>:<GiftOutlined/>}</div><div><h3>{product?.name}</h3><p><b>{number(price)} 积分{!offer&&Number(product?.price_max)>price?'起':''}</b><span>{offer?`库存 ${number(preflight?.stock??offer.stock)}`:'选择城市后显示当地库存'}</span></p></div></div>
      {mode==='demo'&&<Alert type="info" showIcon message="演示兑换仅修改演示数据，不产生真实订单。"/>}
      {offerError&&<Alert type="error" showIcon message={offerError} action={<Button size="small" onClick={()=>setOffersReload(value=>value+1)}>重新加载</Button>}/>}
      <Form layout="vertical" style={{marginTop:20}}>
      {needsCity&&<Form.Item label="兑换城市" required><Select aria-label="兑换城市" showSearch optionFilterProp="label" loading={offersLoading} disabled={busy||offersLoading} value={offerId} onChange={value=>{setOfferId(value);resetVerification()}} placeholder="请选择兑换城市" options={offers.map(value=>({value:value.id,disabled:Number(value.stock)<=0,label:`${value.city} · ${number(value.price)} 积分 · ${Number(value.stock)>0?`库存 ${number(value.stock)}`:'暂无库存'}`}))}/></Form.Item>}
      <Form.Item label="兑换账号" required><Select aria-label="兑换账号" showSearch filterOption={false} disabled={busy} value={aid} onSearch={setQuery} onChange={value=>{setAid(value);resetVerification()}} placeholder="搜索昵称 / 手机号" options={accounts.filter(a=>a.points===null||a.points>=price).map(a=>({value:a.id,label:`${a.nickname} · ${number(a.points)} 积分`}))}/></Form.Item>
      {preflight&&<div className="checkout-check"><span>实时余额 <b>{number(preflight.points)}</b></span><span>实时所需 <b>{number(preflight.price)} 积分</b></span><Button size="small" disabled={busy} onClick={check}>重新核对</Button></div>}
      {preflight?.redemption_kind==='store'&&<Form.Item label="核销门店" required><Select value={storeCode||undefined} onChange={setStoreCode} placeholder="请选择有库存的门店" options={stores.map(s=>({value:s.code,label:s.name+' '+s.address}))}/></Form.Item>}{mode==='live'&&preflight&&smsRequired&&<Form.Item label="平台兑换短信验证码"><Space.Compact style={{width:'100%'}}><Input inputMode="numeric" maxLength={8} value={sms} onChange={e=>setSms(e.target.value)} placeholder="平台要求时填写绑定手机号收到的验证码"/><Button loading={busy} disabled={sentUntil>now} onClick={()=>verify('sms')}>{sentUntil>now?`${Math.ceil((sentUntil-now)/1000)} 秒`:'获取验证码'}</Button></Space.Compact></Form.Item>}
      </Form>
      {notice&&<Alert type="info" showIcon message={notice}/>}
      {challenge&&<iframe ref={frame} title="vivo 官方安全验证" sandbox="allow-scripts allow-forms" src="/captcha/widget" className="official-captcha-frame" onLoad={()=>frame.current?.contentWindow?.postMessage({type:'vivo-init',nonce:challenge.nonce,sdk:challenge.sdk},'*')}/>}
      <p className="form-note">点击确认后仅兑换所选城市的这件商品。网络结果不确定时保留订单并停止重复提交。</p>
    </>}
  </Modal>
}
