import { useEffect,useState } from 'react'
import { Alert,App,Badge,Button,Form,Input,InputNumber,Switch,Tabs,Tag } from 'antd'
import { CheckCircleOutlined,DatabaseOutlined,ReloadOutlined,SaveOutlined,UploadOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { api,type Row } from './api'
import { useWorkspace } from './context'

const number=(value:unknown)=>Number(value||0).toLocaleString('zh-CN')

function Toggle({name,title,description}:{name:string;title:string;description:string}){
  return <div className="settings-toggle"><div><strong>{title}</strong><p>{description}</p></div><Form.Item name={name} valuePropName="checked" noStyle><Switch aria-label={title}/></Form.Item></div>
}

export default function SettingsPage({onImport}:{onImport:()=>void}){
  const {mode,refresh,reload,navigate}=useWorkspace();const {message}=App.useApp();const [form]=Form.useForm()
  const [data,setData]=useState<Row|null>(null);const [engine,setEngine]=useState<Row|null>(null)
  const [error,setError]=useState('');const [saving,setSaving]=useState(false);const [dirty,setDirty]=useState(false);const [tab,setTab]=useState('execution')
  useEffect(()=>{let alive=true;api('/settings',mode).then(value=>{if(alive){setData(value);form.setFieldsValue(value);setDirty(false);setError('')}}).catch(e=>alive&&setError(e.message));return()=>{alive=false}},[mode,refresh,form])
  useEffect(()=>{let alive=true;const load=()=>api('/engine',mode).then(value=>alive&&setEngine(value)).catch(()=>{});load();const timer=setInterval(load,5000);return()=>{alive=false;clearInterval(timer)}},[mode,refresh])
  const values=Form.useWatch([],form)||{}
  const capacity=Number(values.worker_processes??data?.worker_processes??20)*Number(values.async_slots??data?.async_slots??4)
  const exits=Number(data?.proxy_summary?.capacity||0)
  const accountLimit=Math.min(Number(values.concurrency??data?.concurrency??0),capacity,mode==='demo'?256:values.proxy_required===false?1:exits)
  const plannedProcesses=Math.ceil(accountLimit/Number(values.async_slots??data?.async_slots??4))
  async function save(values:Row){setSaving(true);try{await api('/settings',mode,{method:'PUT',body:JSON.stringify(values)});setData(previous=>({...previous,...values}));setDirty(false);message.success('设置已保存');reload()}catch(e){message.error((e as Error).message)}finally{setSaving(false)}}
  const numeric=(name:string,label:string,min:number,max:number,help:string,step=1)=> <Form.Item name={name} label={label} extra={help} rules={[{required:true,message:`请填写${label}`},{type:'number',min,max,message:`取值范围 ${min}–${max}`}]}><InputNumber min={min} max={max} step={step} controls aria-label={label}/></Form.Item>
  return <div className="system-settings">
    <div className="page-head"><div><h1>系统设置</h1><p>管理执行参数、网络连接和账号维护。</p></div><div className="page-actions"><Button icon={<ReloadOutlined aria-hidden/>} disabled={!dirty||saving} onClick={()=>{form.setFieldsValue(data);setDirty(false)}}>恢复已保存配置</Button><Button type="primary" icon={<SaveOutlined aria-hidden/>} loading={saving} disabled={!data} onClick={()=>form.submit()}>保存设置</Button></div></div>
    {error&&<Alert showIcon type="error" message={error} style={{marginBottom:20}}/>}
    <div className="settings-capacity"><div><span>默认账号并发</span><strong>{number(values.concurrency??data?.concurrency)}</strong></div><div><span>进程上限 × 异步槽位</span><strong>{number(values.worker_processes??data?.worker_processes)}<small> × </small>{number(values.async_slots??data?.async_slots)}</strong></div><div><span>可用独立出口</span><strong>{mode==='demo'?'—':number(exits)}</strong></div><p><CheckCircleOutlined/><span>默认批次最多同时处理 <b>{number(accountLimit)}</b> 个账号<small>预计按需启动 {number(plannedProcesses)} 个进程，实际受批次与可用出口限制</small></span></p></div>
    <div className="settings-workspace">
      <section className="panel settings-config"><div className="settings-section-head"><div><h2>运行配置</h2><p>进程与槽位设置用于新批次，请求参数用于后续账号。</p></div>{dirty&&<Tag color="gold">有未保存修改</Tag>}</div>
        <Form form={form} layout="vertical" onValuesChange={()=>setDirty(true)} onFinish={save} onFinishFailed={({errorFields})=>{const key=String(errorFields[0]?.name[0]);setTab(['reference_path'].includes(key)?'data':['panel_sync_minutes'].includes(key)?'maintenance':'execution');message.warning('请检查标记的配置项')}}>
          <Tabs activeKey={tab} onChange={setTab} items={[
            {key:'execution',label:'任务执行',forceRender:true,children:<div className="settings-tab-body"><div className="settings-fields">
              {numeric('worker_processes','进程上限',1,64,'按实际并发按需启动，减少空闲进程占用。')}
              {numeric('async_slots','每进程异步槽位',1,16,'每个槽位处理一个账号，网络等待不占用其他槽位。')}
              {numeric('concurrency','默认账号并发数',1,256,'新建批次的默认值，可在任务中心单独调整。')}
              {numeric('request_gap','同一出口请求间隔（秒）',.2,10,'同一账号的请求统一限速，固定出口不会混用。',.1)}
              {numeric('step_timeout_seconds','单步骤时限（秒）',30,900,'耗时较长的 SDK 和活动步骤至少预留 900 秒。')}
              {numeric('read_retry_count','查询失败重试次数',0,3,'仅重试已确认可安全重发的查询接口。')}
              {numeric('retry_backoff','重试初始等待（秒）',.1,5,'查询重试采用递增等待，避免连续请求。',.1)}
            </div><div className="settings-execution-note"><strong>执行规则</strong><p>账号之间并行，账号内部按依赖顺序执行。领奖、兑换等写操作不会因网络超时自动重发；任务状态通过查询复核。</p></div></div>},
            {key:'connection',label:'代理与连接',forceRender:true,children:<div className="settings-tab-body settings-switch-list">
              <Toggle name="proxy_required" title="真实任务必须使用代理" description="使用账号绑定的固定出口。没有可用代理时等待，不回退直连。"/>
              <Toggle name="proxy_auto_switch" title="不可用代理自动切换" description="原代理连接失败、停用或到期时，为后续执行分配可用出口。待核实提交及平台验证不会触发切换。"/>
              <Toggle name="public_proxy_auth_approved" title="代理认证请求授权" description="允许已登记代理转发真实账号的登录与认证请求。"/>
              <Toggle name="allow_http_panels" title="允许应用宝 HTTP 取码" description="允许连接使用 HTTP 的应用宝端点，供新增与续期使用。"/>
              <Button onClick={()=>navigate('proxies')}>前往代理管理</Button>
            </div>},
            {key:'maintenance',label:'自动维护',forceRender:true,children:<div className="settings-tab-body settings-switch-list">
              <Toggle name="auto_panel_sync" title="自动同步应用宝账号" description="按间隔扫描新增账号，保留在线与离线账号并去重入库。"/>
              <Toggle name="auto_renew" title="小程序到期前自动续期" description="使用账号来源的应用宝端点续期小程序凭证；有暂停批次时不自动执行。"/>
              <Toggle name="auto_app_session_check" title="定期核验官网 App 会话" description="每天检查已导入的 App Cookie；失效后提示重新导入，不伪造或自动转换登录态。有暂停批次时不自动执行。"/>
              <div className="settings-fields settings-maintenance-interval">{numeric('panel_sync_minutes','面板同步间隔（分钟）',15,1440,'任务执行时等待空闲，不挤占当前批次。')}</div>
              <Button onClick={()=>navigate('panels')}>前往应用宝管理</Button>
            </div>},
            {key:'data',label:'数据接入',forceRender:true,children:<div className="settings-tab-body">
              <Form.Item name="reference_path" label="本地缓存导入目录（可选）" extra="仅在手动导入旧缓存时使用。留空即可，运行任务无需原项目目录。"><Input placeholder="可留空，或填写服务器上的缓存目录"/></Form.Item>
              <div className="settings-data-actions"><Button icon={<UploadOutlined/>} onClick={onImport}>导入账号数据</Button><Button onClick={()=>navigate('products')}>前往商品目录</Button></div>
              <div className="settings-execution-note"><strong>迁移与保存</strong><p>服务器部署时迁移整个 data 目录，包含数据库、账号凭证密钥与访问口令文件。任务断点、积分和订单会随数据保留。</p></div>
            </div>},
          ]}/>
        </Form>
      </section>
      <aside className="settings-sidebar">
        <section className="panel"><div className="settings-section-head"><h2>运行状态</h2><Badge status={data?.adapter_available?'success':'warning'} text={data?.adapter_available?'已就绪':'加载中'}/></div><dl className="settings-status-list">
          <div><dt>数据工作区</dt><dd><Tag color={mode==='live'?'blue':'gold'}>{mode==='live'?'真实账号':'演示数据'}</Tag></dd></div>
          <div><dt>执行引擎</dt><dd>多进程 · 异步槽位</dd></div><div><dt>已启动进程</dt><dd>{number(engine?.worker_processes)}</dd></div><div><dt>执行中的账号</dt><dd>{number(engine?.busy_accounts??engine?.busy_processes)}</dd></div>
          <div><dt>已保存断点</dt><dd>{number(engine?.checkpoint_count)}</dd></div><div><dt>数据库</dt><dd>SQLite · WAL</dd></div><div><dt>账号凭证</dt><dd>Fernet 本地加密</dd></div><div><dt>服务时间</dt><dd>{data?.server_time?dayjs(data.server_time).format('MM-DD HH:mm'):'—'}</dd></div>
        </dl></section>
        <section className="panel settings-links"><div className="settings-section-head"><h2>管理入口</h2><DatabaseOutlined/></div><button onClick={()=>navigate('tasks')}><span>任务中心<small>查看队列、执行明细与失败原因</small></span><span>→</span></button><button onClick={()=>navigate('panels')}><span>应用宝管理<small>管理端点、账号新增与续期</small></span><span>→</span></button><button onClick={()=>navigate('proxies')}><span>代理管理<small>查看出口、延迟及账号绑定</small></span><span>→</span></button></section>
      </aside>
    </div>
  </div>
}
