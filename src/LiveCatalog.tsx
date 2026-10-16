import { useEffect,useState } from 'react'
import { Alert,App,Button,Form,Modal,Progress,Radio,Select } from 'antd'
import { SyncOutlined } from '@ant-design/icons'
import { api,type Row } from './api'
import { useWorkspace } from './context'

export function CatalogJob(){
  const {mode,reload}=useWorkspace();const [job,setJob]=useState<Row|null>(null)
  useEffect(()=>{if(mode!=='live')return;let active=true;const load=()=>api('/catalog/jobs',mode).then(r=>{if(active)setJob(r.items[0]||null)}).catch(()=>{});load();const timer=setInterval(load,3000);return()=>{active=false;clearInterval(timer)}},[mode])
  if(!job)return null
  const running=['queued','running'].includes(job.status)
  return <Alert className="import-status" type={running?'info':job.status==='completed'?'success':'warning'} showIcon message={`商品实时更新 · ${running?'进行中':job.status==='completed'?'已完成':'未全部完成'}`} description={<><div>已处理 {job.processed} / {job.total||'—'} 个商品范围，更新 {job.upserted} 条，失败 {job.failed} 项。{job.error}</div>{running&&<Progress percent={job.total?Math.round(job.processed/job.total*100):0} size="small"/>}</>} action={!running&&<Button size="small" onClick={async()=>{if(['failed','partial','paused'].includes(job.status))await api(`/catalog/jobs/${job.id}/resume`,mode,{method:'POST',body:'{}'});reload()}}>{job.status==='completed'?'刷新列表':'继续未完成范围'}</Button>}/>
}

export function LiveCatalogButton(){
  const {mode,reload}=useWorkspace();const {message}=App.useApp();const [open,setOpen]=useState(false);const [accounts,setAccounts]=useState<Row[]>([]);const [query,setQuery]=useState('');const [aid,setAid]=useState<number>();const [scope,setScope]=useState('all');const [busy,setBusy]=useState(false)
  useEffect(()=>{if(!open)return;let active=true;api(`/accounts?status=active&page_size=50&sort=points&q=${encodeURIComponent(query)}`,mode).then(r=>{if(active)setAccounts(r.items)}).catch(()=>{});return()=>{active=false}},[open,query,mode])
  async function start(){if(!aid){message.warning('请选择用于查询的账号');return}setBusy(true);try{await api('/catalog/refresh',mode,{method:'POST',body:JSON.stringify({account_id:aid,scope})});message.success('已开始后台更新商品数据');setOpen(false);reload()}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  if(mode!=='live')return null
  return <><Button type="primary" icon={<SyncOutlined/>} onClick={()=>setOpen(true)}>实时更新商品</Button><Modal title="实时更新商品" open={open} onCancel={()=>setOpen(false)} onOk={start} confirmLoading={busy} okText="开始后台更新" cancelText="取消"><Alert type="info" showIcon message="使用所选账号查询平台当前价格、库存与上下架状态，不兑换商品、不扣积分。"/><Form layout="vertical" style={{marginTop:20}}><Form.Item label="查询账号" required><Select showSearch filterOption={false} value={aid} onSearch={setQuery} onChange={setAid} placeholder="搜索账号昵称或手机号" options={accounts.map(a=>({value:a.id,label:`${a.nickname} · ${a.phone||a.external_id}`}))}/></Form.Item><Form.Item label="更新范围"><Radio.Group value={scope} onChange={e=>setScope(e.target.value)}><Radio value="all">全国商品＋所有城市</Radio><Radio value="national">仅全国商品</Radio></Radio.Group></Form.Item></Form><p className="form-note">全部城市更新会在后台分范围执行。失败范围可继续更新，原数据不会因单次查询失败被清空。</p></Modal></>
}
