import { useState } from 'react'
import { Alert,Button,Input,Modal,Table,Tag,Tabs } from 'antd'
import { UnorderedListOutlined } from '@ant-design/icons'
import { api,type Row } from './api'
import { useWorkspace } from './context'

export default function TaskCoverage(){
  const {mode}=useWorkspace();const [open,setOpen]=useState(false);const [rows,setRows]=useState<Row[]>([]);const [removed,setRemoved]=useState<Row[]>([]);const [tab,setTab]=useState('active');const [query,setQuery]=useState('');const [error,setError]=useState('')
  async function show(){setOpen(true);setTab('active');try{const data=await api('/tasks/catalog',mode);setRows(data.items);setRemoved(data.removed||[]);setError('')}catch(e){setError((e as Error).message)}}
  return <><Button aria-label="任务覆盖清单" icon={<UnorderedListOutlined aria-hidden/>} onClick={show}>任务覆盖清单</Button><Modal title="vivo 任务覆盖清单" open={open} onCancel={()=>setOpen(false)} footer={null} width={880}>
    <Alert showIcon type="info" message={removed.length?'当前只自动执行可完成的任务。暂时移除的项目可在单独清单查看，历史记录和已到账积分保留。':'自动执行免费任务，并按平台返回的进度核验。设备会话、安装、观看或身份资格不足时，保留明确原因；消耗积分的活动默认关闭。'} style={{marginBottom:16}}/>
    {error&&<Alert type="error" message={error}/>}
    {removed.length>0&&<Tabs activeKey={tab} onChange={v=>{setTab(v);setQuery('')}} items={[{key:'active',label:`可执行任务 (${rows.length})`},{key:'removed',label:`已暂时移除 (${removed.length})`}]}/>}
    <Input.Search aria-label="搜索任务" placeholder="搜索任务名称" allowClear onChange={e=>setQuery(e.target.value)} style={{marginBottom:16}}/>
    <Table<Row> rowKey="key" size="small" dataSource={(tab==='removed'?removed:rows).filter(r=>r.name.includes(query))} pagination={{pageSize:10,showSizeChanger:false}} scroll={{x:650}} columns={[{title:'任务',dataIndex:'name',width:290},{title:'执行方式',dataIndex:'default_enabled',width:110,render:(v,r)=><Tag color={v?'blue':'default'}>{v?'自动核验':r.temporarily_removed?'暂时移除':r.key.startsWith('sdk_')?'已合并':'默认关闭'}</Tag>},{title:tab==='removed'?'移除原因':'前置条件',dataIndex:'requirements'}]}/>
  </Modal></>
}
