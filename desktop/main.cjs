// Weave Thinker 桌面壳 — Electron 主进程。
//
// 架构（对应评估报告 docs/AUDIT_2026-09-23_multiclient_stack.md 方案 A）：
//   - SPA 由 app:// 自定义协议供给（SPA fallback 到 index.html，history 路由可用）
//   - 原生能力：dialog / fs / shell 经 ipcMain.handle 暴露，preload 以
//     contextBridge 挂到 window.NativeBridge；兼容桥 window.WeaverNoteApp
//   - 远程 API：SPA 侧由 VITE_API_BASE 指向后端（构建期注入，见 config.toml）
//
// 冒烟模式（script/linux/smoke_desktop.sh 调用）：
//   electron . --smoke --screenshot=<png> --fixture=<abs txt>
//   → 校验 #app 挂载 / NativeBridge 读写列目录 / capturePage 截图 / 退出码
const { app, protocol, BrowserWindow, ipcMain, dialog, shell, net, Menu, clipboard } = require('electron')
const { buildContextMenuTemplate } = require('./context-menu.cjs')

// 测试隔离（评审 I1）：WEAVE_USER_DATA_DIR 指定独立 userData——测试/冒烟用
// 临时 profile，绝不污染开发者日常 profile（~/Library/Application Support/…）。
if (process.env.WEAVE_USER_DATA_DIR) {
  app.setPath('userData', process.env.WEAVE_USER_DATA_DIR)
}
const path = require('node:path')
const fs = require('node:fs')
const fsp = require('node:fs/promises')
const crypto = require('node:crypto')

const ROOT = path.resolve(__dirname, '..')
const DIST = path.join(ROOT, 'app', 'dist')

const isSmoke = process.argv.includes('--smoke')
// 窗口探针（测试接缝）需显式环境门控，防打包版被本地命令误入测试路径（评审 Minor#4）
const isWindowProbe = process.argv.includes('--window-probe') && process.env.WEAVE_WINDOW_PROBE === '1'
// 右键菜单探针（2026-09-30 波，COV-1 测试接缝）：同构 window-probe 门控
const isContextMenuProbe = process.argv.includes('--context-menu-probe') && process.env.WEAVE_CONTEXT_MENU_PROBE === '1'
// macOS dock 激活探针（2026-11-05）：关闭窗口 → 发 activate → 必须重建窗口。
const isActivateProbe = process.argv.includes('--activate-probe') && process.env.WEAVE_ACTIVATE_PROBE === '1'
const argValue = (name) => {
  const hit = process.argv.find((a) => a.startsWith(`--${name}=`))
  return hit ? hit.slice(name.length + 3) : null
}

// ---------------------------------------------------------------------------
// W39 冒烟探针（行为级证据；只在本地栈模式跑，dev 冒烟跳过）
// ---------------------------------------------------------------------------

// 原始 TCP 发一条 WebSocket Upgrade 握手，只读 HTTP 状态行。
// 为什么要自己写而不复用浏览器 WebSocket：浏览器把「握手被拒」压成 onerror，
// 403（路由在但鉴权拒绝）与 404（路由不存在）不可区分；本探针要的正是这个区分。
function wsUpgradeStatus(host, port, requestPath, extraHeaders = {}, timeoutMs = 4000) {
  const nodeNet = require('node:net')
  return new Promise((resolve) => {
    const sock = nodeNet.connect({ host, port })
    let buf = ''
    let done = false
    const finish = (value) => {
      if (done) return
      done = true
      try { sock.destroy() } catch { /* ignore */ }
      resolve(value)
    }
    sock.setTimeout(timeoutMs, () => finish({ status: 0, error: 'timeout' }))
    sock.on('connect', () => {
      const key = Buffer.from(Array.from({ length: 16 }, (_, i) => i + 1)).toString('base64')
      const extra = Object.entries(extraHeaders || {})
        .map(([k, v]) => `${k}: ${v}\r\n`).join('')
      sock.write(
        `GET ${requestPath} HTTP/1.1\r\n` +
        `Host: ${host}:${port}\r\n` +
        'Upgrade: websocket\r\n' +
        'Connection: Upgrade\r\n' +
        `Sec-WebSocket-Key: ${key}\r\n` +
        'Sec-WebSocket-Version: 13\r\n' +
        extra +
        '\r\n'
      )
    })
    sock.on('data', (chunk) => {
      buf += chunk.toString('latin1')
      const m = buf.match(/^HTTP\/1\.[01] (\d{3})/)
      if (m) finish({ status: Number(m[1]) })
    })
    sock.on('error', (e) => finish({ status: 0, error: String((e && e.code) || e) }))
    sock.on('close', () => finish({ status: 0, error: 'closed-before-status' }))
  })
}

// 打包态侧车 BUILD_ID（构建期由 package_macos.sh 写入；dev 无此文件则为 ''）。
function readSidecarBuildId() {
  const cands = [
    path.join(process.resourcesPath || '', 'sidecar', 'weave-server', '_internal', 'BUILD_ID'),
    path.join(ROOT, 'dist_sidecar', 'weave-server', '_internal', 'BUILD_ID')
  ]
  for (const p of cands) {
    try {
      if (p && fs.existsSync(p)) return fs.readFileSync(p, 'utf8').trim()
    } catch { /* ignore */ }
  }
  return ''
}

// 冒烟探针用的本地账号（隔离 profile 的全新 DB ⇒ 首次运行 register，其后 login）。
// 口令**按 profile 随机生成一次**并落该 profile 的 0600 文件（不硬编码、可跨重复冒烟复用）；
// 只读取 access_token，不落盘、不打印。注意：语音 WS 的鉴权契约本身是 `?token=`，
// 服务端 access log 会记录该 URL —— 这是产品既有契约，本探针只在**一次性 profile** 内使用，
// 残余风险已登记（见 tests/verification_log.md W39 iter 3 + docs 审计 §6.2）。
function resolveLocalStackHome() {
  // 与 desktop/local-stack.cjs 同源：探针账号存在**这个目录里的 DB**，
  // 所以口令文件必须跟着它走（否则「新 userData + 旧 DB」→ 登录 401 假红）。
  if (process.env.WEAVE_LOCALSTACK_HOME) return process.env.WEAVE_LOCALSTACK_HOME
  return path.join(app.getPath('userData'), 'localstack')
}

function probePassword(profileDir) {
  const pwFile = path.join(profileDir, '.w39_probe_pw')
  try {
    if (fs.existsSync(pwFile)) {
      const prev = fs.readFileSync(pwFile, 'utf8').trim()
      if (prev) return prev
    }
  } catch { /* fall through to regenerate */ }
  const pw = ['w39', 'smoke', crypto.randomBytes(12).toString('hex')].join('-')
  try {
    fs.mkdirSync(profileDir, { recursive: true })
    fs.writeFileSync(pwFile, pw + '\n', { mode: 0o600 })
  } catch { /* profile may be read-only — still usable this run */ }
  return pw
}

async function mintProbeToken(apiBase) {
  const base = String(apiBase || '').replace(/\/+$/, '')
  if (!base) return ''
  const creds = { username: 'w39_smoke_probe', password: probePassword(resolveLocalStackHome()) }
  // 每次请求独立 signal（复用同一个 AbortSignal 会在第一次超时后把后续调用一并 abort）
  const post = (url, body, timeoutMs = 5000) => fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body,
    signal: AbortSignal.timeout(timeoutMs)
  })
  const login = async () => {
    try {
      const r = await post(base + '/api/auth/login', JSON.stringify(creds))
      if (!r.ok) return ''
      const j = await r.json().catch(() => ({}))
      return String(j.access_token || '')
    } catch { return '' }
  }
  let tok = await login()
  if (tok) return tok
  try {
    const reg = await post(base + '/api/auth/register', JSON.stringify(creds))
    if (!reg.ok && reg.status !== 409) return ''
  } catch { return '' }
  return login()
}

const { createLocalStack } = require('./local-stack.cjs')
const localStack = createLocalStack(console)

// 单实例（2026-09-29 评审 Important）：双开会让两个实例争同一 data-dir/端口，
// 失败方清理可能误伤对方 postmaster——首实例持锁，二次启动仅聚焦既有窗口。
const gotSingleInstanceLock = app.requestSingleInstanceLock()
if (!gotSingleInstanceLock) {
  app.quit()
} else {
  app.on('second-instance', () => {
    // macOS dock / 二次启动：窗口可能已被用户关闭，不能只 focus 已销毁的引用。
    focusOrCreateWindow()
  })
}
let mainWindow = null
let windowCreating = false
let quitting = false
// macOS 关闭窗口后 app 仍在 dock（window-all-closed 不 quit）：再次点 dock 图标
// 时 Electron 只发 activate，必须显式重建/聚焦窗口；否则表现为“点图标没反应”。
async function focusOrCreateWindow() {
  if (quitting || isSmoke || isWindowProbe || isContextMenuProbe) return
  if (!app.isReady()) return
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore()
    if (!mainWindow.isVisible()) mainWindow.show()
    mainWindow.focus()
    return
  }
  if (windowCreating) return
  windowCreating = true
  try {
    await createWindow()
  } catch (e) {
    console.error('WINDOW_RECREATE_FAILED ' + String((e && e.stack) || e))
  } finally {
    windowCreating = false
  }
}
// 右键菜单探针的记录槽（COV-1 测试接缝，非产品路径）：context-menu 处理器在
// 探针模式下把 params 摘要 + 菜单模板登记于此，runContextMenuProbe 汇总落盘。
const contextMenuProbeRecords = []
let contextMenuProbeScenario = ''
let contextMenuProbePopupNext = false  // popup-visual 场景专用：允许一次真实弹出
const skipLocalStack = process.env.WEAVE_SKIP_LOCAL_STACK === '1'
const localRequested = !skipLocalStack &&
  (process.argv.includes('--local') || localStack.config.enabled === true)
let localReady = false
let runtimeApiBase = '' // 仅本地栈成功启动后设置；远程/失败模式保持空 → SPA 用烘焙 base

protocol.registerSchemesAsPrivileged([
  { scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true, stream: true } }
])

const MIME = {
  '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2',
  '.ttf': 'font/ttf', '.otf': 'font/otf', '.wasm': 'application/wasm', '.map': 'application/json',
  '.txt': 'text/plain', '.mp3': 'audio/mpeg', '.mp4': 'video/mp4', '.webm': 'video/webm', '.wav': 'audio/wav'
}

function assertAbs(p) {
  if (typeof p !== 'string' || !path.isAbsolute(p)) throw new Error('absolute path required')
}

function isInside(base, p) {
  const rel = path.relative(base, p)
  return rel !== '' && !rel.startsWith('..') && !path.isAbsolute(rel)
}

function assertTrustedSender(event) {
  const frame = event && event.senderFrame
  if (!frame || frame.parent) throw new Error('untrusted sender frame')
  if (!/^app:\/\/weave\//.test(frame.url)) throw new Error('untrusted sender origin')
}

function registerAppProtocol() {
  protocol.handle('app', async (request) => {
    const url = new URL(request.url)
    let pathname
    try {
      pathname = decodeURIComponent(url.pathname)
    } catch {
      return new Response('bad request', { status: 400 })
    }
    if (pathname.includes('\0')) return new Response('bad request', { status: 400 })
    const resolved = path.normalize(path.join(DIST, pathname))
    if (resolved !== DIST && !resolved.startsWith(DIST + path.sep)) {
      return new Response('forbidden', { status: 403 })
    }
    let filePath = resolved
    try {
      const stat = await fsp.stat(filePath)
      if (stat.isDirectory()) filePath = path.join(filePath, 'index.html')
    } catch {
      // API 路径绝不回退 HTML：否则相对调用会把 index.html 当 200 静默吞掉
      if (pathname === '/api' || pathname.startsWith('/api/')) {
        return new Response(JSON.stringify({ detail: 'api not reachable from app:// origin — set VITE_API_BASE (config.toml)' }), {
          status: 404,
          headers: { 'content-type': 'application/json' }
        })
      }
      if (path.extname(pathname)) return new Response('not found', { status: 404 })
      filePath = path.join(DIST, 'index.html') // SPA fallback（history 路由）
    }
    try {
      const data = await fsp.readFile(filePath)
      const type = MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream'
      return new Response(data, { headers: { 'content-type': type } })
    } catch {
      return new Response('not found', { status: 404 })
    }
  })
}

function registerBridgeIpc() {
  ipcMain.handle('bridge:pickDirectory', async (event) => {
    assertTrustedSender(event)
    const r = await dialog.showOpenDialog({ properties: ['openDirectory', 'createDirectory'] })
    return r.canceled || r.filePaths.length === 0 ? null : r.filePaths[0]
  })
  ipcMain.handle('bridge:pickFile', async (event, filters) => {
    assertTrustedSender(event)
    const r = await dialog.showOpenDialog({ properties: ['openFile'], filters: Array.isArray(filters) ? filters : undefined })
    return r.canceled || r.filePaths.length === 0 ? null : r.filePaths[0]
  })
  ipcMain.handle('bridge:readTextFile', async (event, p) => {
    assertTrustedSender(event)
    assertAbs(p)
    return fsp.readFile(p, 'utf8')
  })
  ipcMain.handle('bridge:writeTextFile', async (event, p, content) => {
    assertTrustedSender(event)
    assertAbs(p)
    if (typeof content !== 'string') throw new Error('content must be a string')
    if (isInside(DIST, p)) throw new Error('refusing to write inside app bundle')
    await fsp.writeFile(p, content, 'utf8')
    return true
  })
  ipcMain.handle('bridge:listDir', async (event, p) => {
    assertTrustedSender(event)
    assertAbs(p)
    const entries = await fsp.readdir(p, { withFileTypes: true })
    return entries.map((e) => ({ name: e.name, isDir: e.isDirectory() }))
  })
  ipcMain.handle('bridge:reveal', async (event, p) => {
    assertTrustedSender(event)
    assertAbs(p)
    shell.showItemInFolder(p)
    return true
  })
  ipcMain.handle('bridge:openExternal', async (event, url) => {
    assertTrustedSender(event)
    if (!/^https?:\/\//.test(String(url))) throw new Error('http(s) url required')
    await shell.openExternal(url)
    return true
  })
  // 兼容桥同步 openUrl（fire-and-forget）
  ipcMain.on('bridge:openExternalFire', (event, url) => {
    try {
      assertTrustedSender(event)
      if (/^https?:\/\//.test(String(url))) shell.openExternal(String(url)).catch(() => {})
    } catch { /* ignore untrusted */ }
  })
  ipcMain.handle('bridge:saveFileBase64', async (event, base64Data, filename) => {
    assertTrustedSender(event)
    const b64 = String(base64Data || '')
    if (b64.length > 300 * 1024 * 1024) throw new Error('file too large')
    const target = path.join(app.getPath('downloads'), path.basename(String(filename || 'download.bin')))
    await fsp.writeFile(target, Buffer.from(b64, 'base64'))
    return target
  })
  ipcMain.handle('bridge:downloadToFile', async (event, url, filename) => {
    assertTrustedSender(event)
    if (!/^https?:\/\//.test(String(url))) throw new Error('absolute http(s) url required')
    const res = await net.fetch(url)
    if (!res.ok) throw new Error(`download failed: ${res.status}`)
    const declared = Number(res.headers.get('content-length') || 0)
    if (declared > 500 * 1024 * 1024) throw new Error('file too large')
    const buf = Buffer.from(await res.arrayBuffer())
    if (buf.length > 500 * 1024 * 1024) throw new Error('file too large')
    const target = path.join(app.getPath('downloads'), path.basename(String(filename || 'download.bin')))
    await fsp.writeFile(target, buf)
    return target
  })
  ipcMain.handle('bridge:appInfo', (event) => {
    assertTrustedSender(event)
    return ({
    name: app.getName(),
    version: app.getVersion(),
    platform: process.platform,
    electron: process.versions.electron
  })
  })
  ipcMain.handle('bridge:isFullScreen', (event) => {
    assertTrustedSender(event)
    const win = BrowserWindow.fromWebContents(event.sender)
    return win ? win.isFullScreen() : false
  })
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    show: false,
    backgroundColor: '#ffffff',
    title: 'WeaveThinker',
    // topbar 波（2026-09-25）：darwin 隐藏原生标题栏保留红绿灯（VSCode mac 同款），
    // 标题栏由 SPA AppTitleBar.vue 自绘（皮肤令牌随主题/深浅切换）。
    // Win/Linux 需 titleBarOverlay/自绘控制钮——本波登记 deferred，保持原生框。
    ...(process.platform === 'darwin' ? { titleBarStyle: 'hiddenInset' } : {}),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // 运行时 API base（2026-09-29 端口协商波）：仅在本地栈成功启动后注入
      // （评审 Critical：无条件注入会让远程/跳过模式指向 localhost:8159）。
      // W26：同分支注入执行模式 —— runtimeApiBase 非空 ⟺ 本地栈实启成功且非
      // 外部模式（:795），是「本地执行」的唯一权威信号源；云端/远程连接不注入。
      ...(runtimeApiBase ? { additionalArguments: [`--weave-api-base=${runtimeApiBase}`, '--weave-exec-mode=local'] } : {})
    }
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url)) shell.openExternal(url).catch(() => {})
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('app://weave/')) {
      event.preventDefault()
      if (/^https?:\/\//.test(url)) shell.openExternal(url).catch(() => {})
    }
  })
  // 右键系统菜单（2026-09-30 用户缺陷：Electron 无默认右键菜单 → 右键"无效"、
  // 无法复制粘贴）。role 菜单走 webContents 内建动作（系统剪贴板/拼写检查原生行为）。
  // 渲染层 @contextmenu.prevent（NotesList/NotebooksList/ConversationRow/表格菜单）
  // 会阻断本事件 → 自定义菜单区域天然不双弹（jupyterlab-desktop 实测语义）。
  win.webContents.on('context-menu', (event, params) => {
    const template = buildContextMenuTemplate(params || {}, { clipboard })
    if (isContextMenuProbe) {
      // 探针模式：登记取证（场景名由 runContextMenuProbe 设定）；实测
      // Menu.buildFromTemplate 而不 popup（真弹会阻塞探针）——模板对但
      // build 不出来的回归不再逃逸（A4.9 评审 B#7）。
      const built = Menu.buildFromTemplate(template)
      contextMenuProbeRecords.push({
        scenario: contextMenuProbeScenario,
        params: {
          isEditable: !!(params && params.isEditable),
          selectionText: (params && params.selectionText) || '',
          linkURL: (params && params.linkURL) || '',
          mediaType: (params && params.mediaType) || 'none',
        },
        template: template.map((i) => ({ role: i.role || null, label: i.label || null, type: i.type || null })),
        built: built.items.map((i) => i.label || i.role || 'separator'),
      })
      if (!contextMenuProbePopupNext) return // 默认不真弹（防阻塞）；popup-visual 场景放行
      contextMenuProbePopupNext = false
      built.popup({ window: win })
      return
    }
    if (!template.length) return // 空场景不弹
    Menu.buildFromTemplate(template).popup({ window: win })
  })
  win.webContents.on('render-process-gone', (_e, details) => {
    console.error('SMOKE_RENDER_GONE ' + JSON.stringify(details))
  })
  // 真全屏波（2026-09-28 用户指令）：全屏状态转发——SPA 自绘 topbar 必须随系统
  // 标题栏同步隐藏（否则全屏后仍显示 38px 自绘栏 = 假全屏）。
  win.on('enter-full-screen', () => {
    if (!win.isDestroyed()) win.webContents.send('bridge:fullScreenChanged', true)
  })
  win.on('leave-full-screen', () => {
    if (!win.isDestroyed()) win.webContents.send('bridge:fullScreenChanged', false)
  })
  // 启动即最大化（2026-09-28 用户指令）：先 maximize 后 show，规避先小窗后跳变。
  // 评审 Minor#3：show 移入 finally 兜底——loadURL 失败也显示窗口（原行为），
  // maximize 在个别 WM 上可能抛错，不影响窗口可见。
  try {
    await win.loadURL('app://weave/index.html')
  } finally {
    if (!isSmoke) {
      try { win.maximize() } catch { /* WM 差异容忍 */ }
      if (!win.isDestroyed()) win.show()
    }
  }
  mainWindow = win
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null
  })
  return win
}

async function runSmoke(win) {
  const fixture = argValue('fixture') || path.join(ROOT, 'tests', 'fixtures', 'bridge_fixture.txt')
  const shot = argValue('screenshot') || path.join(ROOT, 'tests', 'screenshots', 'desktop_login.png')
  const fixtureDir = path.dirname(fixture)

  await new Promise((r) => setTimeout(r, 1800)) // 路由守卫 + 字体/首屏

  const probe = await win.webContents.executeJavaScript(`(async () => {
    const out = {
      mounted: !!(document.querySelector('#app') && document.querySelector('#app').children.length > 0),
      title: document.title,
      bridge: typeof window.NativeBridge,
      weaverCompat: typeof window.WeaverNoteApp
    }
    try {
      out.read = await window.NativeBridge.fs.readTextFile(${JSON.stringify(fixture)})
      await window.NativeBridge.fs.writeTextFile(${JSON.stringify(fixture + '.out')}, 'write-ok')
      out.wrote = true
      const entries = await window.NativeBridge.fs.listDir(${JSON.stringify(fixtureDir)})
      out.listed = Array.isArray(entries) ? entries.length : -1
      out.appInfo = await window.NativeBridge.app.info()
      out.runtimeApiBase = (window.weaveRuntime && window.weaveRuntime.apiBase) || ''
      const r = await fetch('/api/__smoke_probe__')
      out.apiProbe = { status: r.status, ct: r.headers.get('content-type') || '' }
      const t1 = await fetch('app://weave/..%2fdist-evil/secret')
      out.traversalProbe = { status: t1.status }
      // W39：SPA 构建世代（打包脚本烘入 VITE_BUILD_ID）
      out.buildId = (typeof window.__WEAVE_BUILD_ID__ === 'string') ? window.__WEAVE_BUILD_ID__ : ''
      if (out.runtimeApiBase) {
        try {
          const hr = await fetch(out.runtimeApiBase + '/')
          out.runtimeHealth = { status: hr.status, ok: hr.ok }
        } catch (e) { out.runtimeHealth = { error: String((e && e.message) || e) } }
      }
    } catch (e) {
      out.bridgeError = String((e && e.message) || e)
    }
    return out
  })()`)

  const img = await win.webContents.capturePage()
  fs.mkdirSync(path.dirname(shot), { recursive: true })
  fs.writeFileSync(shot, img.toPNG())

  // W39 行为级探针（仅本地栈模式；dev 冒烟 WEAVE_SKIP_LOCAL_STACK=1 时记 localProbes=skipped）：
  //   ① 语音路由可达性 —— **有效 token → 101**（路由存在 + 鉴权通过）；无 token → 403（鉴权门）；
  //      未知路由 → 403（负控，证明 101 非「全路径都通」）。
  //      ⚠ 101 由 accept() 发出，早于读取 audio_protocol ⇒ 本探针**不**主张参数接线；
  //      参数接线由 server/tests/test_voice_ws_protocol_wiring.py 钉（含「删传参必须红」变异验证）。
  //   ② 同源世代 —— SPA 的 __WEAVE_BUILD_ID__ 必须等于侧车 _internal/BUILD_ID
  let voiceRoute = null
  const sidecarBuildId = readSidecarBuildId()
  if (localRequested && runtimeApiBase) {
    try {
      const u = new URL(runtimeApiBase)
      const port = Number(u.port || (u.protocol === 'https:' ? 443 : 80))
      const runtimeBuildId = await (async () => {
        // 运行中侧车自报 build_id（/api/client-exec/status）；比文件更权威
        try {
          const tok = await mintProbeToken(runtimeApiBase)
          if (!tok) return ''
          const r = await fetch(runtimeApiBase.replace(/\/+$/, '') + '/api/client-exec/status', {
            headers: { Authorization: 'Bearer ' + tok },
            signal: AbortSignal.timeout(5000)
          })
          if (!r.ok) return ''
          const j = await r.json().catch(() => ({}))
          return String(j.build_id || '')
        } catch { return '' }
      })()
      const token = await mintProbeToken(runtimeApiBase)
      // 用 Authorization: Bearer 头而不是 ?token= —— 服务端 `extract_websocket_token`
      // 支持两条路（query 优先、header 兜底）；走 header 可避免 access log 落 JWT。
      const auth = token ? { Authorization: 'Bearer ' + token } : {}
      voiceRoute = {
        tokenMinted: !!token,
        authVia: token ? 'header' : 'none',
        runtimeBuildId,
        // ① 有 token → 语音路由必须 **101**（路由存在 + 鉴权通过；101 由 accept() 发出，
        //    早于读取 audio_protocol ⇒ 不主张参数接线）
        voice: await wsUpgradeStatus(u.hostname, port, '/api/voice/ws?audio_protocol=epoch_v1', auth),
        // ② 无 token → 必须 403（鉴权门生效，不是无条件放行）
        voiceNoToken: await wsUpgradeStatus(u.hostname, port, '/api/voice/ws?audio_protocol=epoch_v1'),
        // ③ 对照：同一鉴权打**不存在**的路由 → 403（证明 101 不是「全路径都通」）
        control: await wsUpgradeStatus(u.hostname, port, '/api/voice/__w39_not_a_route__', auth)
      }
    } catch (e) {
      voiceRoute = { error: String((e && e.message) || e) }
    }
  }

  const readOk = typeof probe.read === 'string' && probe.read.trim() === 'weave-bridge-ok'
  const checks = {
    mounted: probe.mounted === true,
    bridge: probe.bridge === 'object',
    weaverCompat: probe.weaverCompat === 'object',
    read: readOk,
    wrote: probe.wrote === true,
    listed: typeof probe.listed === 'number' && probe.listed > 0,
    apiFallback: !!probe.apiProbe && probe.apiProbe.status === 404 && /application\/json/.test(probe.apiProbe.ct),
    traversal403: !!probe.traversalProbe && probe.traversalProbe.status === 403,
    localHealth: !localRequested || localReady,
    // 运行时 API base 链路（2026-09-29 端口协商波）：local 模式下渲染进程必须
    // 拿到注入 base 且能连上（证明协商端口真的被 SPA 使用，而非烘焙的旧端口）
    // 外部/跳过模式 runtimeApiBase='' 合法（评审 low：不得判假红）
    runtimeBase: !localRequested || runtimeApiBase === ''
      || (probe.runtimeApiBase === runtimeApiBase && probe.runtimeApiBase !== ''),
    runtimeReachable: !localRequested || runtimeApiBase === ''
      || !!(probe.runtimeHealth && probe.runtimeHealth.ok === true),
    // W39 ①：语音路由在包内**可达**（有效 token → 101 = 路由在 + 鉴权通过）、鉴权门生效、
    //          负控（未知路由 → 403）。注意 101 不证明 query 参数接线，见上方注释。
    voiceRouteReachable: !localRequested || !runtimeApiBase
      || !!(voiceRoute && voiceRoute.tokenMinted && voiceRoute.voice && voiceRoute.voice.status === 101),
    voiceRouteAuthGate: !localRequested || !runtimeApiBase
      || !!(voiceRoute && voiceRoute.voiceNoToken && voiceRoute.voiceNoToken.status === 403),
    voiceRouteControl: !localRequested || !runtimeApiBase
      || !!(voiceRoute && voiceRoute.control && voiceRoute.control.status === 403),
    // 探针真的跑了（而不是因缺 runtimeApiBase 而空转）；空转时 localHealth 已失败
    voiceProbesRan: !localRequested || !runtimeApiBase || !!(voiceRoute && !voiceRoute.error),
    // W39 ②：SPA 与侧车同一构建世代（同一 BID 单源注入，拒绝「新侧车 + 旧 SPA」混装）
    buildIdSameGeneration: !localRequested || !runtimeApiBase
      || (!!probe.buildId && probe.buildId !== 'dev' && probe.buildId === sidecarBuildId),
    // m5：SPA 世代 == **运行中侧车自报** build_id（文件核对之外的活体核对）
    runtimeBuildIdMatch: !localRequested || !runtimeApiBase
      || !!(voiceRoute && voiceRoute.runtimeBuildId === probe.buildId),
    screenshot: fs.existsSync(shot) && fs.statSync(shot).size > 1000
  }
  const pass = Object.values(checks).every(Boolean)
  console.log('SMOKE_RESULT ' + JSON.stringify({
    pass,
    checks,
    screenshot: shot,
    screenshotBytes: fs.existsSync(shot) ? fs.statSync(shot).size : 0,
    voiceRoute,
    localProbes: (localRequested && runtimeApiBase) ? 'ran' : 'skipped (no local stack)',
    buildId: { spa: probe.buildId || '', sidecar: sidecarBuildId },
    probe
  }))
  return pass
}


// macOS dock 激活探针（2026-11-05）：关闭窗口 → 模拟 dock 点击（app.emit('activate')）
// → 必须重建一个可加载的窗口。仅测试接缝（WEAVE_ACTIVATE_PROBE=1 门控）。
async function runActivateProbe(win) {
  if (process.platform !== 'darwin') {
    console.log('ACTIVATE_PROBE_RESULT ' + JSON.stringify({ skipped: 'non-darwin' }))
    return true
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  const closed = new Promise((r) => win.once('closed', r))
  win.close()
  await closed
  const afterClose = BrowserWindow.getAllWindows().length
  app.emit('activate')
  let recreated = null
  for (let i = 0; i < 60; i++) {
    await sleep(100)
    const wins = BrowserWindow.getAllWindows()
    if (wins.length === 1 && wins[0] !== win && !wins[0].isDestroyed()) {
      recreated = wins[0]
      break
    }
  }
  if (recreated) {
    // 等重建窗口完成 app:// 加载（与 createWindow 的 loadURL 对齐）
    for (let i = 0; i < 50; i++) {
      const url = recreated.webContents.getURL()
      if (url.startsWith('app://weave/')) break
      await sleep(100)
    }
    // createWindow 在 loadURL 之后才赋 mainWindow；等它落定再断言。
    for (let i = 0; i < 30; i++) {
      if (mainWindow && !mainWindow.isDestroyed()) break
      await sleep(100)
    }
  }
  const out = {
    afterClose,
    recreated: !!recreated,
    windowCount: BrowserWindow.getAllWindows().length,
    mainWindow: !!mainWindow && !mainWindow.isDestroyed(),
    url: recreated ? recreated.webContents.getURL() : null
  }
  console.log('ACTIVATE_PROBE_RESULT ' + JSON.stringify(out))
  return out.recreated && out.windowCount === 1 && out.mainWindow
}

// 窗口状态探针（tests/ui_window_state.py 驱动；COV-1 测试接缝，非产品路径）：
//   WEAVE_WINDOW_PROBE=1 electron . --window-probe --screenshot-dir=<dir>
//   （环境门控必需，评审 Minor#4——防打包版被误入测试路径）
//   → 启动态 / maximize / fullscreen（setFullScreen 与菜单 toggle 双路径）/ 恢复
//     逐态 bounds + contentBounds + 显示器几何 + DOM（自绘 topbar/--wt-titlebar-h）
//     + 全屏整屏像素探针 + 截图。
async function runWindowProbe(win) {
  const { screen } = require('electron')
  const { execFileSync } = require('node:child_process')
  const shotDir = argValue('screenshot-dir') || path.join(ROOT, 'tests', 'screenshots')
  fs.mkdirSync(shotDir, { recursive: true })
  const disp = screen.getPrimaryDisplay()
  const out = { display: { bounds: disp.bounds, workArea: disp.workArea }, states: {} }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  const probeStrip = (file) => {
    try {
      const code = [
        'import json',
        'from PIL import Image',
        'im = Image.open(' + JSON.stringify(file) + ').convert("RGB")',
        'w,h = im.size',
        'pts = {"tl": (6,6), "tm": (w//2, 8), "y40": (200, 40), "y60": (200, 60), "mid": (w//2, h//2), "below": (200, 130)}',
        'print("PIX " + json.dumps({**{k: im.getpixel(v) for k,v in pts.items()}, "frameMax": max(c[1] for c in im.getextrema())}))'
      ].join('\n')
      const r = execFileSync('/usr/bin/python3', ['-c', code], { encoding: 'utf8' }).trim()
      return JSON.parse(r.slice(4))
    } catch (e) { return { err: String(e).slice(0, 120) } }
  }
  const domState = async () => {
    try {
      return await win.webContents.executeJavaScript(`(() => {
        const el = document.querySelector('[data-testid="app-titlebar"]')
        return {
          titlebar: !!el,
          titlebarH: getComputedStyle(document.documentElement).getPropertyValue('--wt-titlebar-h').trim(),
          innerH: window.innerHeight
        }
      })()`)
    } catch (e) { return { err: String(e).slice(0, 120) } }
  }
  const snap = async (key) => {
    out.states[key] = {
      bounds: win.getBounds(),
      contentBounds: win.getContentBounds(),
      isMaximized: win.isMaximized(),
      isFullScreen: win.isFullScreen(),
      isSimpleFullScreen: win.isSimpleFullScreen(),
      dom: await domState()
    }
  }
  const shot = async (key) => {
    const img = await win.webContents.capturePage()
    fs.writeFileSync(path.join(shotDir, 'win_' + key + '.png'), img.toPNG())
    const scr = path.join(shotDir, 'screen_' + key + '.png')
    // 评审 M1：先删旧文件再截——测试端据「存在」即知本 run 新鲜产出（防陈旧假绿）
    try { fs.rmSync(scr, { force: true }) } catch (e) { /* ignore */ }
    let pixels = null
    let attempts = 0
    // FCV#2 flake 修复：全屏动画竞态——重试截屏直至顶栏条呈系统黑条（上限 3 次）。
    // 遗留#6（2026-10-03）活性门：整帧近全黑（screencapture 失败黑帧）不得判 dark 成立
    // ——否则「顶栏条为系统黑条」在黑帧上空真。frameMax≤30 视为无效捕获。
    while (attempts < 3) {
      attempts++
      try { execFileSync('screencapture', ['-x', scr]) } catch (e) { /* ignore */ }
      if (!key.startsWith('fullscreen')) break
      pixels = probeStrip(scr)
      // 活性=整帧非黑 且 顶条以下区域（below/mid）有亮度——纯黑帧与「亮壁纸残影
      // + 未入全屏的暗顶条」都不得判 dark 成立（复审 Minor：整帧 max 会被壁纸骗过）
      const below = [...(pixels.below || []), ...(pixels.mid || [])]
      const alive = !pixels.err && (pixels.frameMax ?? 0) > 30 &&
        below.length > 0 && Math.max(...below) > 20
      const dark = alive && Array.isArray(pixels.tl) && Array.isArray(pixels.tm) &&
        Math.max(...pixels.tl) < 60 && Math.max(...pixels.tm) < 60
      if (dark) break
      await sleep(700)
    }
    if (key.startsWith('fullscreen')) {
      out.states[key].screenShot = scr
      out.states[key].screenShotFresh = fs.existsSync(scr) &&
        (Date.now() - fs.statSync(scr).mtimeMs) < 30000
      out.states[key].screenShotAttempts = attempts
      out.states[key].pixels = pixels || probeStrip(scr)
    }
  }
  const enterFullScreen = async (viaMenu) => {
    // 遗留#6（2026-10-03）：全屏转换失败的真因是显示器休眠（见 tests 唤醒预检）——
    // 此处保持单发（转换中重复 setFullScreen 会取消转换，勿加重试）。
    const entered = new Promise((r) => win.once('enter-full-screen', r))
    if (viaMenu) Menu.sendActionToFirstResponder('toggleFullScreen:')
    else win.setFullScreen(true)
    await Promise.race([entered, sleep(6000)])
    await sleep(1400)
  }
  const leaveFullScreen = async () => {
    const left = new Promise((r) => win.once('leave-full-screen', r))
    try {
      if (win.isFullScreen()) Menu.sendActionToFirstResponder('toggleFullScreen:')
      // 评审 Low#5：非 key 窗口下 toggle 静默 no-op → 300ms 后仍未退出则 API 直退
      setTimeout(() => {
        if (!win.isDestroyed() && win.isFullScreen()) win.setFullScreen(false)
      }, 300)
    } catch (e) { win.setFullScreen(false) }
    await Promise.race([left, sleep(6000)])
    await sleep(900)
  }

  await sleep(2200)
  win.focus()
  await snap('initial'); await shot('initial')

  win.maximize()
  await sleep(900)
  await snap('maximized'); await shot('maximized')
  win.unmaximize()
  await sleep(900)
  await snap('normal')

  await enterFullScreen(false)
  await snap('fullscreen_setfs'); await shot('fullscreen_setfs')
  await leaveFullScreen()
  await snap('after_setfs_leave')

  await enterFullScreen(true)
  await snap('fullscreen_menu'); await shot('fullscreen_menu')
  await leaveFullScreen()
  await snap('restored')

  console.log('WINDOW_PROBE_RESULT ' + JSON.stringify(out))
}

// 右键探针的 mock API（判据#10）：protocol.handle('http'/'https') 直接拦截
// 服务 API 请求回夹具 JSON——仅探针模式调用；不走 webRequest 重定向（XHR 拦跨
// scheme 重定向，实测教训）。
function installProbeApiMock() {
  const cors = {
    'access-control-allow-origin': '*',
    'access-control-allow-methods': 'GET,POST,PUT,DELETE,OPTIONS',
    'access-control-allow-headers': 'authorization,content-type',
  }
  const ts = '2026-01-01T00:00:00'
  const msg = (id, role, content) => ({ id, conversation_id: 'c-old', role, content, created_at: ts, reasoning_content: null, tool_calls: null, tool_results: null, context_info: null })
  const tableMd = '# 表格夹具\n\n| A | B |\n|---|---|\n| 1 | 2 |\n'
  const routes = {
    '/api/conversations': [{ id: 'c-old', title: 'OLD-conv', group_id: null, sort_order: 0, created_at: ts, updated_at: ts, assistant_id: 'a1' }],
    '/api/conversations/groups': [],
    '/api/conversations/c-old': { id: 'c-old', title: 'OLD-conv', group_id: null, sort_order: 0, created_at: ts, updated_at: ts, assistant_id: 'a1', deathmatch_mode: false, messages: [msg('m1', 'user', 'probe-msg')] },
    '/api/assistants': [{ id: 'a1', name: '默认助手', created_at: ts, updated_at: ts }],
    '/api/notes/notebooks': [{ id: 'nb-1', name: 'NB-1', created_at: ts, updated_at: ts }],
    '/api/notes/notebooks/nb-1/notes': [{ id: 'n-1', notebook_id: 'nb-1', title: '表格夹具', created_at: ts, updated_at: ts }],
    '/api/notes/notes/n-1': { id: 'n-1', notebook_id: 'nb-1', title: '表格夹具', content: tableMd, raw_transcription: null, created_at: ts, updated_at: ts },
    '/api/skins': { skins: [] },
    '/api/skins/mine': { skins: [] },
    '/api/sync/status': { server_enabled: true, client_enabled: true, device_id: 'd', push_cursor: 0, pull_cursor: 0, pending_events: 0, last_sync_at: ts, last_error: null, conflicts_count: 0, resync_pending: false },
    '/api/sync/account': { bound: true, server_url: 'http://cloud.test', cloud_username: 'cu', device_id: 'd', last_sync_at: ts, last_error: null },
    '/api/sync/devices': [],
    '/api/sync/conflicts': [],
  }
  const handler = async (request) => {
    const url = new URL(request.url)
    if (request.method === 'OPTIONS') {
      return new Response('', { status: 204, headers: cors })
    }
    let body = routes[url.pathname]
    if (body === undefined) {
      const m = url.pathname.match(/\/api\/conversations\/([^/]+)\/messages$/)
      body = m ? [msg('m1', 'user', 'probe-msg')] : {}
    }
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { ...cors, 'content-type': 'application/json' },
    })
  }
  protocol.handle('http', handler)
  protocol.handle('https', handler)
}

// 右键菜单探针（tests/ui_context_menu.py 驱动；COV-1 测试接缝，非产品路径）：
//   WEAVE_CONTEXT_MENU_PROBE=1 electron . --context-menu-probe
//   → 页面注入三场景夹具（input 可编辑 / 选中文本 / preventDefault 元素），
//     逐场景合成右键（sendInputEvent 真实输入管线），处理器记录 params+模板，
//     汇总 CONTEXT_MENU_PROBE_RESULT 行 + tests/context_menu_probe.json 落盘。
async function runContextMenuProbe(win) {
  const out = { scenarios: [] }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
  await sleep(2200) // 首屏/路由守卫对齐 runWindowProbe 惯例

  const pos = await win.webContents.executeJavaScript(`(() => {
    const wrap = document.createElement('div')
    wrap.id = 'probe-fixtures'
    wrap.style.cssText = 'position:fixed;left:24px;top:120px;z-index:99999;background:#fff;padding:12px;display:flex;flex-direction:column;gap:12px'
    const input = document.createElement('input')
    input.id = 'probe-input'
    input.value = 'probe-editable-text'
    input.style.cssText = 'width:280px'
    const sel = document.createElement('div')
    sel.id = 'probe-sel'
    sel.textContent = 'probe selectable text'
    const cust = document.createElement('button')
    cust.id = 'probe-cust'
    cust.textContent = 'custom menu zone'
    cust.addEventListener('contextmenu', (e) => e.preventDefault()) // 模拟 @contextmenu.prevent
    wrap.append(input, sel, cust)
    document.body.appendChild(wrap)
    const r = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) } }
    return { input: r(input), sel: r(sel), cust: r(cust) }
  })()`)

  const rightClick = async (p) => {
    win.webContents.sendInputEvent({ type: 'mouseDown', x: p.x, y: p.y, button: 'right', clickCount: 1 })
    win.webContents.sendInputEvent({ type: 'mouseUp', x: p.x, y: p.y, button: 'right', clickCount: 1 })
    await sleep(500)
  }
  const pick = (name) => {
    const recs = contextMenuProbeRecords.filter((r) => r.scenario === name)
    return recs.length ? recs[recs.length - 1] : null
  }

  // 场景 a：input 可编辑区（select() 全选 → canCut/canCopy；剪贴板就绪 → canPaste）
  clipboard.writeText('probe-clip')
  await win.webContents.executeJavaScript(
    `(() => { const el = document.getElementById('probe-input'); el.focus(); el.select(); return true })()`)
  contextMenuProbeScenario = 'input-editable'
  await rightClick(pos.input)
  const ra = pick('input-editable')
  out.scenarios.push(ra
    ? { name: 'input-editable', event_received: true, params: ra.params, template: ra.template }
    : { name: 'input-editable', event_received: false, params: {}, template: [] })

  // 场景 b：非编辑区选中文本（Range API 选中）
  await win.webContents.executeJavaScript(`(() => {
    const el = document.getElementById('probe-sel')
    const range = document.createRange(); range.selectNodeContents(el)
    const s = getSelection(); s.removeAllRanges(); s.addRange(range)
    return true })()`)
  contextMenuProbeScenario = 'selection'
  await rightClick(pos.sel)
  const rb = pick('selection')
  out.scenarios.push(rb
    ? { name: 'selection', event_received: true, params: rb.params, template: rb.template }
    : { name: 'selection', event_received: false, params: {}, template: [] })

  // 场景 c：渲染层 preventDefault → 主进程不得收到 context-menu（判据#3 不双弹语义）
  contextMenuProbeScenario = 'prevented'
  await rightClick(pos.cust)
  const rc = pick('prevented')
  out.scenarios.push(rc
    ? { name: 'prevented', event_received: true, params: rc.params, template: rc.template }
    : { name: 'prevented', event_received: false, params: {}, template: [] })

  // ── 阶段 F（判据#10，A4.9 A#7/B#7）：真实组件右键 E2E ──────────────────
  // protocol.handle('http'/'https') 拦截 API 回夹具（webRequest 跨 scheme
  // 重定向会被 XHR 拦下——实测教训）+ 种登录态 + 重载 → 真实 ChatLayout。
  installProbeApiMock()
  await win.webContents.executeJavaScript(`(() => {
    localStorage.setItem('chatllm_token', 'probe-token')
    localStorage.setItem('chatllm_user', JSON.stringify({ id: 'u1', username: 'probe',
      created_at: '2026-01-01T00:00:00', agent_permissions: {}, nickname: null, avatar_data: null }))
    return true })()`)
  const domProbe = async (js) => {
    try { return await win.webContents.executeJavaScript(js) } catch (e) { return { err: String(e).slice(0, 120) } }
  }
  const rightClickSel = async (name, selector, menuSel) => {
    await win.webContents.loadURL('app://weave/index.html')
    await sleep(2200)
    const p = await domProbe(`(() => {
      const el = document.querySelector(${JSON.stringify(selector)})
      if (!el) return null
      el.scrollIntoView({ block: 'center' })
      const b = el.getBoundingClientRect()
      return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) }
    })()`)
    if (!p) {
      out.scenarios.push({ name, event_received: false, params: {}, template: [], dom: { found: false } })
      return
    }
    contextMenuProbeScenario = name
    await sleep(200)
    await rightClick(p)
    const rec = pick(name)
    const dom = await domProbe(`(() => ({
      found: true,
      customMenuVisible: ${menuSel ? `!!document.querySelector(${JSON.stringify(menuSel)})` : 'null'},
    }))()`)
    out.scenarios.push(rec
      ? { name, event_received: true, params: rec.params, template: rec.template, dom }
      : { name, event_received: false, params: {}, template: [], dom })
  }

  // F1 真实 ChatInput（contenteditable）→ 系统菜单含 paste
  await rightClickSel('real-chatinput', '.chat-input-wrapper [contenteditable="true"]', null)
  // F2 真实 ConversationRow（@contextmenu.prevent）→ 系统菜单不弹
  await rightClickSel('real-conversation-row', '.conversation-item', null)
  // F3 真实 NotesList 笔记行（自定义菜单组件）→ 自定义菜单在位 + 不双弹
  await win.webContents.loadURL('app://weave/notes/nb-1')
  await sleep(2400)
  const pNote = await domProbe(`(() => {
    const el = document.querySelector('.note-row, .note-item')
    if (!el) return null
    el.scrollIntoView({ block: 'center' })
    const b = el.getBoundingClientRect()
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) }
  })()`)
  if (pNote) {
    contextMenuProbeScenario = 'real-noteslist'
    await rightClick(pNote)
    const rec = pick('real-noteslist')
    // 判据#10 产品行为（wave-2 评审裁决）：右键即开自定义菜单（桌面原生语义）——
    // 右键同时测「菜单在位」与「系统菜单不双弹」两面。
    await sleep(400)
    const dom = await domProbe(`(() => ({
      found: true,
      customMenuVisible: !!document.querySelector('.context-menu'),
    }))()`)
    out.scenarios.push(rec
      ? { name: 'real-noteslist', event_received: true, params: rec.params, template: rec.template, dom }
      : { name: 'real-noteslist', event_received: false, params: {}, template: [], dom })
  } else {
    out.scenarios.push({ name: 'real-noteslist', event_received: false, params: {}, template: [], dom: { found: false } })
  }
  // F4 真实表格单元格（WysiwygEditor 表格菜单）→ 菜单在位 + 不双弹
  await win.webContents.loadURL('app://weave/notes/nb-1/n-1')
  await sleep(2400)
  const pCell = await domProbe(`(() => {
    const el = document.querySelector('.wysiwyg-editor table td')
    if (!el) return null
    const b = el.getBoundingClientRect()
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) }
  })()`)
  if (pCell) {
    contextMenuProbeScenario = 'real-table-cell'
    await rightClick(pCell)
    const rec = pick('real-table-cell')
    const dom = await domProbe(`(() => ({
      found: true,
      customMenuVisible: !!document.querySelector('.table-context-menu'),
    }))()`)
    out.scenarios.push(rec
      ? { name: 'real-table-cell', event_received: true, params: rec.params, template: rec.template, dom }
      : { name: 'real-table-cell', event_received: false, params: {}, template: [], dom })
  } else {
    out.scenarios.push({ name: 'real-table-cell', event_received: false, params: {}, template: [], dom: { found: false } })
  }

  // popup-visual：真实菜单弹出取证（用户缺陷场景复刻——真实 handler popup 路径 +
  // 整屏 screencapture；这是「右键完全失败」类报告的第一复现面）
  await win.webContents.loadURL('app://weave/index.html')
  await sleep(2200)
  const pPop = await domProbe(`(() => {
    const ta = document.createElement('textarea')
    ta.id = 'probe-popup-input'
    ta.value = 'popup-visual-text'
    ta.style.cssText = 'position:fixed;left:60px;top:200px;width:320px;height:64px;z-index:99999;background:#fff'
    document.body.appendChild(ta)
    ta.focus(); ta.select()
    const b = ta.getBoundingClientRect()
    return { x: Math.round(b.left + b.width / 2), y: Math.round(b.top + b.height / 2) }
  })()`)
  if (pPop) {
    contextMenuProbeScenario = 'popup-visual'
    contextMenuProbePopupNext = true
    await rightClick(pPop)
    await sleep(700)
    const { execFileSync } = require('node:child_process')
    const scr = path.join(ROOT, 'tests', 'screenshots', 'context_menu_popup.png')
    try { execFileSync('screencapture', ['-x', scr]) } catch (e) { /* screencapture 失败不阻断 */ }
    out.popupShot = scr
    out.popupShotExists = fs.existsSync(scr) && (Date.now() - fs.statSync(scr).mtimeMs) < 30000
    // 退出挂死修复（实测三轮）：原生菜单模态追踪期间，win.close() 与 app.exit 均
    // 被拦截——必须先让菜单消失。macOS 语义：焦点丢失即关 popup menu。
    try { win.blur() } catch (e) { /* ignore */ }
    await sleep(600)
  }

  try {
    fs.writeFileSync(path.join(ROOT, 'tests', 'context_menu_probe.json'), JSON.stringify(out, null, 2))
  } catch (e) { console.error('probe write failed: ' + String(e)) }
  console.log('CONTEXT_MENU_PROBE_RESULT ' + JSON.stringify(out))
  // 探针硬退出（实测两轮挂死）：popup-visual 的原生菜单模态追踪会拦截 app.exit
  // （win.close() 亦不足）——测试接缝直接 process.exit，仅探针路径生效；
  // 本地栈清理由 whenReady 的 stop 调用承担（探针约定 WEAVE_SKIP_LOCAL_STACK=1，
  // stop 秒回无泄漏；非跳过部署模式下探针使用者自担栈清理）。
  process.exit(0)
}

app.whenReady().then(async () => {
  // 单实例锁失败：只允许 quit，绝不再走启动路径（评审 low：曾可能短暂拉起 PG/后端）
  if (!gotSingleInstanceLock) return
  registerAppProtocol()
  registerBridgeIpc()

  const showLocalStackError = async (tag, payload, text) => {
    console.error(`${tag} ` + JSON.stringify(payload))
    // 失败也落盘（Finder 启动时 stdout 不可见；下次事故可查）
    try {
      const logPath = path.join(app.getPath('userData'), 'localstack', 'backend_sidecar.log')
      fs.appendFileSync(logPath, `\n[${new Date().toISOString()}] ${tag} ${JSON.stringify(payload)}\n`)
    } catch { /* 落盘失败不阻断 */ }
    if (isSmoke || isWindowProbe) return
    const logPath = path.join(app.getPath('userData'), 'localstack', 'backend_sidecar.log')
    const tried = localStack.getBackendPort()
    const hint = String(payload.stage || '') === 'health'
      ? `后端未在 :${tried} 就绪。端口可能被其它程序占用，请关闭占用程序后重启应用。`
      : '请关闭可能占用端口的程序后重启应用。'
    try {
      dialog.showErrorBox(
        '本地服务启动失败',
        `阶段：${payload.stage || 'exception'}\n端口：${tried}\n${text.slice(-1200)}\n\n${hint}\n日志：${logPath}`
      )
    } catch { /* 对话框失败不阻断 */ }
  }

  if (localRequested) {
    try {
      const result = await localStack.start()
      localReady = result.ok
      if (result.ok) {
        if (!result.external && result.stage !== 'skipped') runtimeApiBase = result.apiBase
        console.log('LOCAL_STACK_READY ' + JSON.stringify({ apiBase: result.apiBase, external: Boolean(result.external) }))
      } else {
        await localStack.stop() // 部分失败清理（PG 可能已起）
        await showLocalStackError('LOCAL_STACK_FAILED', result, String(result.out || result.apiBase || ''))
      }
    } catch (e) {
      await localStack.stop() // 抛出路径同样清理（I2）
      await showLocalStackError('LOCAL_STACK_ERROR', { stage: 'exception' }, String((e && e.stack) || e))
    }
  }

  try {
    // W36：初始创建也纳入 windowCreating 闸，防 macOS 启动/加载期 activate
    // 并发再建第二个窗口（focusOrCreateWindow 只在创建函数内部置位）。
    windowCreating = true
    const win = await createWindow()
    windowCreating = false
    if (isSmoke) {
      const ok = await runSmoke(win)
      await localStack.stop()
      app.exit(ok ? 0 : 1)
    } else if (isWindowProbe) {
      await runWindowProbe(win)
      await localStack.stop()
      app.exit(0)
    } else if (isContextMenuProbe) {
      await runContextMenuProbe(win)
      await localStack.stop()
      app.exit(0)
    } else if (isActivateProbe) {
      const ok = await runActivateProbe(win)
      await localStack.stop()
      app.exit(ok ? 0 : 1)
    }
  } catch (e) {
    console.error('SMOKE_FATAL ' + String((e && e.stack) || e))
    await localStack.stop()
    app.exit(1)
  }
})

let localQuitHandled = false
app.on('before-quit', (event) => {
  quitting = true
  if (localReady && !localQuitHandled) {
    event.preventDefault()
    localQuitHandled = true
    localStack.stop().finally(() => app.quit())
  }
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin' || isSmoke) app.quit()
})

// macOS dock 图标点击 / 应用重新激活：窗口被关闭后必须重建（见 focusOrCreateWindow）。
app.on('activate', () => {
  focusOrCreateWindow()
})
