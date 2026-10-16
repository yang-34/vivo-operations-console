import {useState} from 'react'
import {App,Button,InputNumber,Modal,Radio,Space} from 'antd'
import {KeyOutlined} from '@ant-design/icons'
import {api} from './api'
import {useWorkspace} from './context'

export default function LoginMaintenance({ids}:{ids:number[]}) {
  const {mode,reload,navigate}=useWorkspace();const {message}=App.useApp()
  const [open,setOpen]=useState(false),[busy,setBusy]=useState(false),[profile,setProfile]=useState('login'),[concurrency,setConcurrency]=useState<number>(3)
  async function start(){setBusy(true);try{await api('/accounts/maintenance',mode,{method:'POST',body:JSON.stringify({profile,account_ids:ids,concurrency})});setOpen(false);reload();navigate('tasks');message.success('已创建所选账号的登录态维护批次')}catch(e){message.error((e as Error).message)}finally{setBusy(false)}}
  return <><Button icon={<KeyOutlined/>} disabled={!ids.length} onClick={()=>setOpen(true)}>登录态维护</Button><Modal title="批量登录态维护" open={open} onCancel={()=>!busy&&setOpen(false)} onOk={start} confirmLoading={busy} okText="开始维护" cancelText="取消">
    <p>仅处理已选择的 {ids.length} 个账号，不执行积分任务或商品兑换。</p>
    <Radio.Group value={profile} onChange={e=>setProfile(e.target.value)}><Space direction="vertical"><Radio value="login">小程序登录态获取 / 续期</Radio><Radio value="app_check">官网 App 会话核验</Radio></Space></Radio.Group>
    <p className="muted">小程序续期使用账号所属应用宝端点；App 核验只检查已导入 Cookie，缺少 Cookie 的账号会单独记录原因。应用宝小程序凭证不会转换成 App 登录态。</p>
    <Space><span>账号并发</span><InputNumber min={1} max={8} value={concurrency} onChange={v=>setConcurrency(v||1)}/></Space>
  </Modal></>
}
