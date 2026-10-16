import { defineConfig } from '@playwright/test'
import path from 'node:path'
const runId=process.env.VIVO_E2E_RUN_ID||String(Date.now())
process.env.VIVO_E2E_RUN_ID=runId
const dataDirectory=path.resolve('artifacts',`e2e-data-${runId}`)

export default defineConfig({
  testDir:'./e2e',
  timeout:120000,
  expect:{timeout:10000},
  workers:1,
  fullyParallel:false,
  reporter:[['list'],['html',{outputFolder:'artifacts/e2e-report',open:'never'}]],
  outputDir:'artifacts/e2e-results',
  metadata:{dataDirectory},
  use:{baseURL:'http://127.0.0.1:8110',browserName:'chromium',channel:'msedge',viewport:{width:1512,height:1080},screenshot:'only-on-failure',trace:'retain-on-failure'},
  webServer:{
    command:'.venv\\Scripts\\python.exe server/app.py',
    url:'http://127.0.0.1:8110/login',
    reuseExistingServer:false,
    timeout:30000,
    env:{VIVO_WEB_PORT:'8110',VIVO_DATA_DIR:dataDirectory,PYTHONIOENCODING:'utf-8'},
  },
})
