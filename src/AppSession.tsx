import { useEffect, useState } from 'react'
import { Alert, App, Button, Checkbox, Descriptions, Input, Modal, Popconfirm, Space, Tag, Upload } from 'antd'
import { KeyOutlined, UploadOutlined } from '@ant-design/icons'
import { api, type Row } from './api'
import { useWorkspace } from './context'

export default function AppSession({accountId}:{accountId?:number}) {
  const {mode,reload}=useWorkspace();const {message}=App.useApp()
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[input,setInput]=useState(''),[ua,setUa]=useState(''),[file,setFile]=useState<Row|null>(null),[filename,setFilename]=useState(''),[create,setCreate]=useState(!accountId),[status,setStatus]=useState<Row|null>(null),[selected,setSelected]=useState(accountId)
  async function refresh(id=selected){if(id)setStatus(await api(`/accounts/${id}/app-session`,mode))}
  useEffect(()=>{if(open&&selected)refresh().catch(e=>message.error(e.message))},[open,selected])
  async function verify(id=selected){if(!id)return;setBusy(true);try{const value=await api(`/accounts/${id}/app-session/verify`,mode,{method:'POST',body:'{}'});message.success(`官网登录态有效，当前 ${value.points.toLocaleString('zh-CN')} 积分`);await refresh(id);reload()}catch(e){message.error((e as Error).message);await refresh(id)}finally{setBusy(false)}}
  async function save(){setBusy(true);try{const value=await api('/app-sessions/import',mode,{method:'POST',body:JSON.stringify({account_id:accountId,input:file||input,user_agent:ua,create_account:create})});setSelected(value.account_id);setInput('');setUa('');setFile(null);setFilename('');reload();await verify(value.account_id)}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  const names:Record<string,string>={missing:'未导入',unverified:'待核验',verified:'登录态有效',invalid:'核验未通过',expired:'已过期'}
  return <><Button icon={<KeyOutlined/>} disabled={mode!=='live'} onClick={()=>setOpen(true)}>{accountId?'官网 App 登录态':'导入 App Cookie'}</Button><Modal title="官网 App 登录态" open={open} onCancel={()=>!busy&&setOpen(false)} footer={null} width={620} destroyOnHidden>
    <p>导入该账号本人的 Cookie，供积分商城、兑换和订单查询使用。小程序登录态独立保存。</p>
    {selected&&<Descriptions size="small" column={1} items={[{key:'account',label:'账号 ID',children:selected},{key:'status',label:'状态',children:<Tag color={status?.status==='verified'?'success':'default'}>{names[status?.status||'missing']}</Tag>},{key:'count',label:'Cookie 数量',children:status?.cookie_count||0},{key:'verified',label:'最近核验',children:status?.verified_at||'—'}]}/>}
    {status?.last_error&&<Alert type="warning" showIcon message={status.last_error} style={{marginBottom:16}}/>}
    <Space wrap style={{margin:'12px 0'}}><Upload accept=".har,.json" showUploadList={false} beforeUpload={async f=>{try{setFile(JSON.parse(await f.text()));setFilename(f.name);setInput('')}catch{message.error('文件不是有效的 HAR / JSON')}return false}}><Button icon={<UploadOutlined/>}>选择 HAR / Cookie JSON</Button></Upload>{filename&&<Tag closable onClose={()=>{setFile(null);setFilename('')}}>{filename}</Tag>}</Space>
    <Input.TextArea rows={5} value={input} disabled={!!file||busy} onChange={e=>setInput(e.target.value)} placeholder="或粘贴 Cookie：vvc_openid=…; vvc_r=…" autoComplete="off" spellCheck={false}/>
    <Input value={ua} disabled={busy} onChange={e=>setUa(e.target.value)} placeholder="User-Agent（HAR 会自动读取）" style={{marginTop:12}} autoComplete="off"/>
    {!accountId&&<Checkbox checked={create} onChange={e=>setCreate(e.target.checked)} style={{marginTop:12}}>未匹配现有账号时创建独立官网 App 账号</Checkbox>}
    <p className="muted">保存时加密，接口不返回 Cookie 原文。核验仅读取登录状态和积分，不兑换商品；平台要求验证码时保留验证流程。</p>
    <Space wrap><Button type="primary" loading={busy} disabled={!file&&!input.trim()} onClick={save}>导入并核验</Button>{selected&&status?.status!=='missing'&&<><Button loading={busy} onClick={()=>verify()}>重新核验</Button><Popconfirm title="移除该账号的官网 App 登录态？" onConfirm={async()=>{await api(`/accounts/${selected}/app-session`,mode,{method:'DELETE'});setStatus(null);await refresh();reload()}}><Button disabled={busy}>移除登录态</Button></Popconfirm></>}</Space>
  </Modal></>
}
