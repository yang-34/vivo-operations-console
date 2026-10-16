import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, App as AntApp } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App'
import './styles.css'
import './catalog.css'
import './settings.css'
import './responsive.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} button={{autoInsertSpace:false}} theme={{token:{colorPrimary:'#2856dc',colorSuccess:'#259777',colorWarning:'#d89528',colorError:'#d65b60',colorText:'#263345',colorTextSecondary:'#7d8797',colorBorder:'#e6eaf0',borderRadius:6,fontFamily:'Inter, "Segoe UI", "Microsoft YaHei", sans-serif',fontSize:13,controlHeight:34},components:{Table:{headerBg:'#f8fafc',headerColor:'#7d8797',cellPaddingBlock:15},Button:{primaryShadow:'none'},Menu:{itemSelectedBg:'#eef3ff',itemSelectedColor:'#2856dc'}}}}><AntApp><App/></AntApp></ConfigProvider></React.StrictMode>,
)
