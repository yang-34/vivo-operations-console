// Capture the real UI using a NEW synthetic database. Never read the live data/ directory.
import { chromium, expect } from '@playwright/test'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import net from 'node:net'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const output = path.join(root, 'docs', 'screenshots')
const artifacts = path.join(root, 'artifacts')
const python = process.env.VIVO_SCREENSHOT_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
if (!existsSync(path.join(root, 'dist', 'index.html'))) throw new Error('Run npm run build first')
await mkdir(artifacts, { recursive: true })
await mkdir(output, { recursive: true })
const dataDirectory = await mkdtemp(path.join(artifacts, 'readme-preview-'))
const socket = net.createServer()
socket.listen(0, '127.0.0.1')
await once(socket, 'listening')
const port = socket.address().port
await new Promise(resolve => socket.close(resolve))
const base = `http://127.0.0.1:${port}`
const env = { ...process.env, VIVO_DATA_DIR: dataDirectory, VIVO_WEB_PORT: String(port), VIVO_BIND_HOST: '127.0.0.1',
  VIVO_ADMIN_PASSWORD: '', VIVO_ADMIN_PASSWORD_HASH: '', VIVO_SESSION_SECRET: '', VIVO_SECURE_COOKIE: '0', VIVO_TRUST_PROXY: '0',
  PYTHONIOENCODING: 'utf-8' }

// All addresses are documentation-only ranges/domains. No imports or platform calls.
const seed = String.raw`
import sys
sys.path.insert(0, 'server')
import app
from store import db, encrypt, now
app.setup()
with db('demo') as c:
    c.execute("UPDATE accounts SET nickname='演示账号 ' || printf('%04d',id), phone='', source='演示账号池'")
    c.execute('UPDATE products SET metadata=? WHERE id=?', ('{"itemId":"readme-coffee","city":"全国"}', 'demo-coffee'))
    for suffix,city,price,stock in [('a','示例甲城',650,80),('b','示例乙城',720,0)]:
        c.execute('''INSERT INTO products(id,name,category,price,stock,art,description,source,metadata,updated_at)
            SELECT ?,name,category,?,?,art,?,'demo',?,? FROM products WHERE id='demo-coffee' ''',
            (f'demo-coffee-city-{suffix}',price,stock,city+' · 演示商品',
             '{"itemId":"readme-coffee","city":"'+city+'"}',now()))
    for i in range(1,9):
        status = 'healthy' if i <= 6 else ('failed' if i == 7 else 'unverified')
        c.execute('INSERT INTO proxies(fingerprint,url,source,status,exit_ip,latency_ms,entry_latency_ms,checked_at,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (f'demo-proxy-{i}', f'socks5h://example-user:example-password@192.0.2.{i}:1080', 'user', status, f'198.51.100.{i}' if i<=6 else '', 210+i*37 if i<=6 else None, 28+i*3, now(), now(), '2030-12-31T23:59:59+08:00'))
    for i in range(1,5):
        c.execute('INSERT INTO panel_sources(fingerprint,url,label,status,account_count,online_count,offline_count,checked_at) VALUES(?,?,?,?,?,?,?,?)',
            (f'demo-panel-{i}',encrypt({'url':f'https://panel-{i}.example.invalid'}),f'演示端点 {i:02d}', 'connected' if i<4 else 'unreachable',1000,960 if i<4 else 0,40 if i<4 else 1000,now()))
    for i in range(1,49):
        c.execute('INSERT INTO proxy_bindings(account_id,proxy_id,created_at) VALUES(?,?,?)',(i,(i-1)%6+1,now()))
`

async function runPython(code) {
  const child = spawn(python, ['-c', code], { cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
  let diagnostic = ''
  child.stdout.on('data', chunk => { diagnostic += chunk })
  child.stderr.on('data', chunk => { diagnostic += chunk })
  const [codeNumber] = await once(child, 'exit')
  if (codeNumber !== 0) throw new Error(`Demo initialization failed: ${diagnostic}`)
}
await runPython(seed)

let browser
let serverError
let serverLog = ''
const server = spawn(python, ['server/app.py'], { cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
const serverExited = once(server, 'exit').catch(() => {})
server.on('error', error => { serverError = error })
server.stdout.on('data', chunk => { serverLog += chunk })
server.stderr.on('data', chunk => { serverLog += chunk })
try {
  let ready = false
  for (let i = 0; i < 150; i++) {
    if (serverError) throw serverError
    if (server.exitCode !== null) throw new Error('Isolated preview server exited')
    try {
      const response = await fetch(`${base}/login`, { signal: AbortSignal.timeout(500) })
      ready = response.ok && response.headers.get('x-vivo-service') === 'vivo-operations-console'
    } catch { /* Wait for this newly spawned server only. */ }
    if (ready) break
    await new Promise(resolve => setTimeout(resolve, 200))
  }
  if (!ready) throw new Error('Isolated preview server did not become ready')
  browser = await chromium.launch({ channel: process.env.VIVO_SCREENSHOT_BROWSER || 'msedge', headless: true })
  const context = await browser.newContext({ viewport: { width: 1600, height: 1060 }, deviceScaleFactor: 1 })
  // Enforce demo mode and block every non-local request, including accidental remote images.
  await context.addInitScript(() => localStorage.setItem('vivo-mode', 'demo'))
  await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto(`${base}/login`)
  await page.locator('#password').fill((await readFile(path.join(dataDirectory, 'admin-password.txt'), 'utf8')).trim())
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.waitForURL(`${base}/`)
  const views = [
    ['dashboard', '运营总览', '.metric-grid'], ['accounts', '账号管理', 'tbody tr.ant-table-row'],
    ['tasks', '任务中心', '.batch-heading'], ['panels', '应用宝管理', 'tbody tr.ant-table-row'],
    ['proxies', '代理管理', 'tbody tr.ant-table-row'], ['products', '积分商城', '.product-card'],
    ['redemptions', '兑换记录', '.order-card'],
  ]
  const results = []
  for (const [route, title, readySelector] of views) {
    await page.goto(`${base}/#/` + route)
    await expect(page.getByRole('heading', { level: 1, name: title, exact: true })).toBeVisible()
    await page.locator(readySelector).first().waitFor()
    await expect(page.locator('.ant-spin-spinning')).toHaveCount(0)
    await page.evaluate(() => document.fonts.ready)
    // Recharts animates paths in JavaScript; let the initial transition finish.
    if (route === 'dashboard') await page.waitForTimeout(1800)
    // Tag all sensitive table columns by their heading, so column reordering stays safe.
    await page.evaluate(() => {
      const sensitive = ['ID', '入口', '实际出口', '代理账号', '代理密码', '订单号', '批次编号', '券码', '兑换码']
      for (const table of document.querySelectorAll('table')) {
        const headings = [...table.querySelectorAll('thead th')]
        for (const row of table.querySelectorAll('tbody tr')) {
          headings.forEach((heading, index) => {
            const cell = row.children[index]
            if (!sensitive.includes(heading.textContent.trim()) || !cell?.textContent.trim()) return
            const existing = cell.querySelector('.endpoint-cell strong, code')
            if (existing) existing.classList.add('readme-sensitive')
            else {
              const span = document.createElement('span')
              span.className = 'readme-sensitive'
              span.style.display = 'inline-block'
              span.append(...cell.childNodes)
              cell.append(span)
            }
          })
        }
      }
    })
    // Opaque screenshot masks contain no recoverable original text. Originals are not saved.
    const mask = page.locator('.readme-sensitive, .account-name, .endpoint-cell small, .proxy-password, .batch-heading p, .recent-order-copy > span, .order-card-meta b, .order-card-meta code, .fulfillment-code')
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`${route}: horizontal overflow`)
    await page.screenshot({ path: path.join(output, `${route}.png`), fullPage: true, animations: 'disabled',
      mask: [mask], maskColor: '#b8c2d1' })
    results.push({ page: route, screenshot: `docs/screenshots/${route}.png`, maskedElements: await mask.count() })
    console.log(`Captured ${route} (isolated demo, identifiers redacted)`)
  }
  if (errors.length) throw new Error(`Browser errors: ${errors.join('; ')}`)
  await writeFile(path.join(dataDirectory, 'capture-report.json'), JSON.stringify({ mode: 'demo', externalNetwork: 'blocked', views: results }, null, 2))
} finally {
  await browser?.close()
  server.kill()
  await serverExited
  await writeFile(path.join(dataDirectory, 'server.log'), serverLog)
}
