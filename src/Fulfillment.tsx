import { Typography } from 'antd'
import dayjs from 'dayjs'

function plain(value:unknown):string{
  return String(value??'').replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi,'').replace(/<br\s*\/?\s*>/gi,'\n').replace(/<\/(?:p|div|li)>/gi,'\n').replace(/<[^>]*>/g,'').replace(/&nbsp;/g,' ').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').trim()
}
export default function Fulfillment({value,compact=false}:{value:unknown;compact?:boolean}){
  if(!value)return null
  let data:Record<string,unknown>
  try{data=typeof value==='string'?JSON.parse(value):value as Record<string,unknown>}catch{data={deliveryInfo:value}}
  if(!data||typeof data!=='object'||Array.isArray(data))data={deliveryInfo:value}
  const seen=new Set<string>();const codes:[string,string][]=[]
  for(const [key,label] of [['redeemCode','激活码'],['activationCode','激活码'],['couponCode','核销码'],['cardNo','卡号'],['cardPwd','卡密']]){
    const code=plain(data[key]);if(code&&!seen.has(code)){seen.add(code);codes.push([label,code])}
  }
  const delivery=plain(data.deliveryInfo)
  if(!codes.length&&delivery&&delivery.length<=80&&!delivery.includes('\n')){codes.push(['兑换信息',delivery]);seen.add(delivery)}
  const expiry=data.expireTime??data.couponExpirationTime
  const when=typeof expiry==='number'?dayjs(expiry<1e11?expiry*1000:expiry):expiry?dayjs(String(expiry)):null
  const instructions=Array.isArray(data.instructions)?data.instructions.map(v=>{const row=v as Record<string,unknown>;return [plain(row.name),plain(row.content)].filter(Boolean).join('：')}).join('\n'):plain(data.instructions)
  if(!codes.length&&!delivery&&!instructions&&!when?.isValid())return null
  return <section className={`fulfillment${compact?' compact':''}`} aria-label="兑换激活信息">
    {codes.map(([label,code])=><div className="fulfillment-code" key={label+code}><span>{label}</span><Typography.Text copyable={{text:code}}>{code}</Typography.Text></div>)}
    {when?.isValid()&&<p className="fulfillment-expiry">有效期至 {when.format('YYYY-MM-DD HH:mm')}</p>}
    {!compact&&delivery&&!seen.has(delivery)&&<p className="fulfillment-instructions">{delivery}</p>}
    {!compact&&instructions&&instructions!==delivery&&<details><summary>使用说明</summary><p className="fulfillment-instructions">{instructions}</p></details>}
  </section>
}
