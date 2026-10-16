import { Table, Tag } from 'antd'
import type { Row } from './api'

const names:Record<string,string>={completed:'已确认完成',failed:'失败',partial:'部分完成',manual:'需实际操作',blocked:'条件未满足',pending:'未完成',skipped:'无可执行项'}
const colors:Record<string,string>={completed:'success',failed:'error',partial:'warning',manual:'warning',blocked:'warning',pending:'default',skipped:'default'}
const routes:Record<string,string>={sign:'业务签到',web:'网页浏览',sdk:'SDK 任务',community:'社区阅读',favorite:'商城收藏',shop_cart:'商城加购',shop_detail:'商品浏览',shop_search:'商城搜索',shop_member:'会员中心访问',ad:'广告任务',wish:'任务上报',activity:'活动任务',manual:'实际操作',eligibility:'资格检查',vivop:'vivo+ 任务'}
function stateOf(r:Row):string{
  if(r.status)return r.status
  if(r.skip||Number(r.code)===20041)return 'skipped'
  if(r.after_signed||r.res==='ok'||r.res==='already'||r.todaySigned)return 'completed'
  const code=r.retcode??r.code
  if(code!=null)return [0,200,10002,10003].includes(Number(code))?'completed':'failed'
  const children=Object.values(r).filter(v=>v&&typeof v==='object'&&!Array.isArray(v)&&(v.status||v.code!=null||v.retcode!=null)) as Row[]
  if(children.length){const states=children.map(stateOf);return states.every(s=>s==='completed')?'completed':states.some(s=>s==='completed')?'partial':'failed'}
  if(r.free===0||r.tasks===0)return 'skipped'
  return 'pending'
}

export function taskStepMessage(step:Row):string{
  const result=step.details?.result
  if(result?.skip)return String(result.skip)
  if(step.step_key==='tasks.discover'&&result?.discovered!=null)return `已发现 ${result.discovered} 项任务，涉及 ${result.packages} 个客户端，完整清单如下。`
  const rows=Array.isArray(result)?result:Array.isArray(result?.results)?result.results:null
  if(rows){if(!rows.length)return '当前没有可执行项。';const completed=rows.filter((r:Row)=>stateOf(r)==='completed').length;return `共 ${rows.length} 项结果，已确认 ${completed} 项，具体状态及原因见下方。`}
  return step.message||''
}

export default function TaskStepDetails({details}:{details:Row|undefined}){
  if(!details||!Object.keys(details).length)return null
  const result=details.result
  const results:Row[]=Array.isArray(result)?result:Array.isArray(result?.results)?result.results:[]
  const issues:Row[]=details.issues||[]
  return <div className="step-details">
    {results.length>0&&<><small className="muted">逐项结果 {results.length} 项 · 已确认 {results.filter(r=>stateOf(r)==='completed').length} 项 · 其余结果及原因如下</small><Table<Row> size="small" rowKey={(_,i)=>String(i)} dataSource={results} pagination={results.length>10?{pageSize:10,showSizeChanger:false}:false} scroll={{x:600}} columns={[
      {title:'任务 / 业务',render:(_,r)=><><b>{r.name||r.biz||r.id||r.tid||'任务'}</b>{r.pkg&&<small className="block muted">{r.pkg}</small>}</>},
      {title:'执行分支',width:105,render:(_,r)=>routes[r.route]||'—'},
      {title:'结果',width:125,render:(_,r)=>{const state=stateOf(r);return <Tag color={colors[state]}>{names[state]||state}</Tag>}},
      {title:'说明 / 返回码',render:(_,r)=>{const reply=r.receive||r.response||r.responses?.at(-1)||r;const code=reply.code??reply.retcode??r.provider_code;return <><span>{r.reason||r.message||r.res||r.reward||'详见完整返回结果'}</span>{r.provider_message&&<small className="block muted">{r.provider_message}</small>}{code!=null&&<small className="block muted">code: {String(code)}{reply.message?` · ${reply.message}`:''}</small>}{r.before!=null&&<small className="block muted">进度 {r.before} → {r.after??'未取得'}{r.unclaimed>0?` · 待领 ${r.unclaimed} 积分`:''}</small>}</>}},
    ]}/></>}
    {issues.length>0&&<details><summary>接口返回明细 · {issues.length} 项</summary><Table<Row> size="small" rowKey={(_,i)=>String(i)} dataSource={issues} pagination={{pageSize:8,showSizeChanger:false}} scroll={{x:600}} columns={[{title:'接口',dataIndex:'endpoint'},{title:'返回码',dataIndex:'code',width:95},{title:'平台说明',dataIndex:'message'}]}/></details>}
    <details><summary>完整结果</summary><pre>{JSON.stringify(result,null,2)}</pre></details>
  </div>
}
