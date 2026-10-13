// 本地全栈编排（Electron 主进程侧）：拉起 embedded PG + 本地后端，健康轮询，退出清理。
//
// 策略：
//   - 开发/未打包：调用 script/*/localstack.sh（venv 形态，已实证）
//   - 打包后：优先使用 resources/sidecar/weave-server（PyInstaller 单二进制）；
//     WEAVE_SIDECAR_BIN 可强制指定（开发期验证打包产物）
// 数据目录：LOCALSTACK_HOME（打包后= userData/localstack；WEAVE_LOCALSTACK_HOME 可覆盖）
// 开关：config.toml [local] → desktop/local.config.json；--local 强制开；WEAVE_SKIP_LOCAL_STACK=1 强制关
const { spawn, spawnSync } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const net = require('node:net')

const ROOT = path.resolve(__dirname, '..')

function electronApp() {
  try { return require('electron').app } catch { return null }
}

function isPackaged() {
  const app = electronApp()
  return Boolean(app && app.isPackaged)
}

function resolveLocalHome() {
  if (process.env.WEAVE_LOCALSTACK_HOME) return process.env.WEAVE_LOCALSTACK_HOME
  // 2026-09-28 事故修复：dev 壳与安装版共享同一数据目录（产品原则=所有端体验
  // 一致）。旧实现 dev 回落 repo/.localstack + 固定 55432 —— 与安装版抢端口换
  // 库，用户账号「时有时无」。测试栈走 script/linux/localstack.sh（独立家目录
  // + 独立端口 55433/8161），永不与用户应用冲突。
  const app = electronApp()
  return path.join(app.getPath('userData'), 'localstack')
}

function resolveScriptsDir() {
  if (isPackaged()) return path.join(process.resourcesPath, 'script')
  return path.join(ROOT, 'script')
}

function resolveServerDir() {
  if (isPackaged()) return path.join(process.resourcesPath, 'server')
  return path.join(ROOT, 'server')
}

function resolveSidecar() {
  if (process.env.WEAVE_SIDECAR_BIN) return process.env.WEAVE_SIDECAR_BIN
  if (isPackaged()) {
    const exe = process.platform === 'win32' ? 'weave-server.exe' : 'weave-server'
    const p = path.join(process.resourcesPath, 'sidecar', 'weave-server', exe)
    if (fs.existsSync(p)) return p
  }
  return null
}

function baseEnv() {
  const env = { ...process.env, LOCALSTACK_HOME: resolveLocalHome() }
  if (isPackaged()) {
    env.LOCALSTACK_SERVER_DIR = path.join(process.resourcesPath, 'server')
    env.LOCALSTACK_CONFIG_DIR = path.join(resolveLocalHome(), 'app')
    const binDir = path.join(process.resourcesPath, 'bin')
    if (fs.existsSync(binDir)) {
      env.PATH = env.PATH ? `${binDir}${path.delimiter}${env.PATH}` : binDir
      // terminal/process 自建受限 PATH 的工具定位（git 等内置工具）
      env.WEAVE_BUNDLED_BIN = binDir
    }
    const browsersDir = path.join(process.resourcesPath, 'browsers')
    if (fs.existsSync(browsersDir)) env.PLAYWRIGHT_BROWSERS_PATH = browsersDir
  }
  return env
}

// 打包形态：把随包 soffice 的绝对路径写回配置（应用移动后每次启动自动刷新）
function ensureBundledConfigPaths(confDir, logger) {
  if (!isPackaged()) return
  const soffice = path.join(process.resourcesPath, 'LibreOffice.app', 'Contents', 'MacOS', 'soffice')
  const cfg = path.join(confDir, 'config.toml')
  if (!fs.existsSync(soffice) || !fs.existsSync(cfg)) return
  try {
    const text = fs.readFileSync(cfg, 'utf8')
    const patched = text.replace(/^soffice_path\s*=.*$/gm, `soffice_path = ${JSON.stringify(soffice)}`)
    if (patched !== text) {
      fs.writeFileSync(cfg, patched, 'utf8')
      logger.log('[local-stack] soffice_path → bundled LibreOffice')
    }
  } catch (e) {
    logger.log(`[local-stack] WARN: soffice_path patch failed: ${e}`)
  }
}

// 协商端口必须回写 config.toml（内部 self-eval/自引用 URL 与 DSN 消费）：
// - [server].port ← 后端实际端口（每次启动）
// - [database].port ← 实际 PG 端口（仅 sidecar 模式传入时；script 模式交给 localstack.sh）
// section 感知逐行替换，避免正则越界误改下一个表的 port（评审 medium）。
function patchConfigPorts(confDir, backendPort, pgPort, logger) {
  const cfg = path.join(confDir, 'config.toml')
  if (!fs.existsSync(cfg)) return
  try {
    const lines = fs.readFileSync(cfg, 'utf8').split('\n')
    let section = ''
    const changed = []
    let dbDone = pgPort == null
    for (let i = 0; i < lines.length; i++) {
      const m = lines[i].match(/^\s*\[([^\]]+)\]\s*$/)
      if (m) { section = m[1].trim(); continue }
      if (/^\s*port\s*=/.test(lines[i])) {
        if (section === 'server') { lines[i] = `port = ${backendPort}`; changed.push('server') }
        else if (section === 'database' && !dbDone) { lines[i] = `port = ${pgPort}`; changed.push('database'); dbDone = true }
      }
    }
    if (changed.length) {
      fs.writeFileSync(cfg, lines.join('\n'), 'utf8')
      logger.log(`[local-stack] config ports patched: ${changed.join(',')} (server=${backendPort}${pgPort != null ? `, db=${pgPort}` : ''})`)
    }
  } catch (e) { logger.log(`[local-stack] WARN: config port patch failed: ${e}`) }
}

function scriptPath(name) {
  return process.platform === 'win32'
    ? path.join(resolveScriptsDir(), 'windows', `${name}.bat`)
    : path.join(resolveScriptsDir(), 'linux', `${name}.sh`)
}

function runScript(name, args = [], opts = {}) {
  const timeoutMs = opts.timeoutMs || 10 * 60 * 1000
  return new Promise((resolve) => {
    const child = spawn(process.platform === 'win32' ? 'cmd' : 'bash',
      process.platform === 'win32' ? ['/c', scriptPath(name), ...args] : [scriptPath(name), ...args],
      { cwd: ROOT, env: { ...baseEnv(), ...(opts.env || {}) } })
    let out = ''
    let done = false
    const timer = setTimeout(() => {
      if (done) return
      child.kill('SIGKILL')
      resolve({ code: -2, out: out + `\n[local-stack] timeout after ${timeoutMs}ms` })
    }, timeoutMs)
    child.stdout.on('data', (d) => { out += d })
    child.stderr.on('data', (d) => { out += d })
    child.on('error', (e) => { done = true; clearTimeout(timer); resolve({ code: -1, out: String(e) }) })
    child.on('close', (code) => { done = true; clearTimeout(timer); resolve({ code, out }) })
  })
}

function isPortFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer()
    srv.once('error', () => resolve(false))
    srv.once('listening', () => srv.close(() => resolve(true)))
    srv.listen(port, '127.0.0.1')
  })
}

// 后端端口协商（2026-09-29 实机事故）：8159 被外来进程（如 chatbot 前端 vite）
// 占用时，旧实现直接 bind 失败→整套本地栈静默死掉。规则：首选空闲→用之；
// 否则向上找空闲端口（≤30 个）；全失败→返回首选由启动失败路径显式上报。
// **不做"复用已运行后端"**：仅凭健康文案无法认证对端身份，任何本地进程都可
// 冒充并成为凭据汇聚点（评审 Critical）——永不 adopt；重复启动由单实例锁挡。
function toPort(v, fallback) {
  const n = Number.parseInt(String(v), 10)
  return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : fallback
}

async function chooseBackendPort(preferred, logger = console, tries = 30) {
  for (let i = 0; i < tries; i++) {
    const port = toPort(preferred, 8159) + i
    if (await isPortFree(port)) {
      if (i > 0) logger.log(`[local-stack] port ${preferred} busy → using free :${port}`)
      return { port }
    }
  }
  logger.log(`[local-stack] WARN: no free backend port in ${preferred}..${Number(preferred) + tries - 1}`)
  return { port: toPort(preferred, 8159) }
}

async function waitHealthy(url, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2500), redirect: 'error' })
      if (res.ok) {
        const text = (await res.text()).slice(0, 4096)
        try {
          const body = JSON.parse(text)
          if (String(body.message || '') === 'Weave Thinker API is running') return true
        } catch { /* 非 JSON */ }
      }
    } catch { /* retry */ }
    await new Promise((r) => setTimeout(r, 1000))
  }
  return false
}

function loadLocalConfig() {
  const p = path.join(__dirname, 'local.config.json')
  // local.config.json 不入包（v0.7 评审裁决：不烘 dev 配置）——打包形态缺省
  // 必须 local-first「安装即完整系统」（v0.6 判据 #6）；dev 无该文件则保持关闭。
  // 回归根因（2026-09-26 打包排查）：缺省 enabled:false 使打包正常启动永不起栈，
  // 且 localHealth 的 `!localRequested || localReady` 空真掩盖了它。
  if (!fs.existsSync(p)) return { enabled: isPackaged(), pgPort: 55432, backendPort: 8159, apiBase: '' }
  try { return JSON.parse(fs.readFileSync(p, 'utf8')) } catch { return { enabled: isPackaged() } }
}

function createLocalStack(logger = console) {
  const cfg = loadLocalConfig()
  const home = resolveLocalHome()
  // env 覆盖（集成测试/高级用户；packaged 缺省仍走 local.config.json）
  const configuredApiBase = cfg.apiBase || ''
  // 本地 base（或未配置）一律参与端口协商——历史上的 dev local.config.json
  // 固定 apiBase=127.0.0.1:8159 曾短路协商，导致端口被占时误连外来进程/假健康
  const isLocalBase = !configuredApiBase
    || /^https?:\/\/(127\.0\.0\.1|localhost)([:\/]|$)/.test(configuredApiBase)
  let activePort = toPort(process.env.WEAVE_LOCAL_BACKEND_PORT, toPort(cfg.backendPort, 8159))
  let activeApiBase = configuredApiBase || `http://127.0.0.1:${activePort}`
  const pgPort = toPort(process.env.WEAVE_LOCAL_PG_PORT, toPort(cfg.pgPort, 55432))
  let pgStarted = false
  let backendStarted = false
  let sidecarChild = null
  let pgChild = null
  const ownedPids = new Set()   // 我们 spawn 过的进程 pid（含 postmaster），仅据此定向回收

  // ---- sidecar（打包形态） ----
  async function sidecarStart(sidecar) {
    if (isLocalBase) {
      const choice = await chooseBackendPort(activePort, logger)
      activePort = choice.port
      activeApiBase = `http://127.0.0.1:${activePort}`
    }
    const dataDir = path.join(home, 'pgdata')
    const stateFile = path.join(home, 'pg_state.json')
    const pg = spawn(sidecar, ['pg', 'start', '--data-dir', dataDir, '--state-file', stateFile, '--port', String(pgPort)],
      { env: baseEnv(), stdio: ['ignore', 'pipe', 'pipe'] })
    pg.on('error', () => { /* 由 ready 超时路径统一处理 */ })
    pgChild = pg
    if (pg.pid) ownedPids.add(pg.pid)
    let pgOut = ''
    const pgOk = await new Promise((resolve) => {
      const t = setTimeout(() => resolve(false), 90000)
      pg.stdout.on('data', (d) => {
        pgOut += d
        if (pgOut.includes('LOCALSTACK_PG_READY')) { clearTimeout(t); resolve(true) }
      })
      pg.stderr.on('data', (d) => { pgOut += d })
      pg.on('exit', () => { clearTimeout(t); resolve(pgOut.includes('LOCALSTACK_PG_READY')) })
    })
    if (!pgOk) return { ok: false, stage: 'pg', out: pgOut }
    pgStarted = true

    const confDir = path.join(home, 'app')
    fs.mkdirSync(confDir, { recursive: true })
    if (!fs.existsSync(path.join(confDir, 'config.toml'))) {
      const gen = spawnSync(sidecar, ['gen-config'], {
        cwd: confDir,
        env: { ...baseEnv(), LOCALSTACK_BACKEND_PORT: String(activePort), LOCALSTACK_CONFIG_DIR: confDir, LOCALSTACK_SERVER_DIR: resolveServerDir() }
      })
      if (gen.status !== 0) {
        return { ok: false, stage: 'config', out: String(gen.stderr || gen.stdout || '') }
      }
    }
    ensureBundledConfigPaths(confDir, logger)
    patchConfigPorts(confDir, activePort, pgPort, logger)
    // 输出必须落盘/读取：管道无人消费会填满缓冲导致后端启动卡死
    const beLog = fs.openSync(path.join(home, 'backend_sidecar.log'), 'a')
    sidecarChild = spawn(sidecar, ['backend'], {
      cwd: confDir,
      env: { ...baseEnv(), LOCALSTACK_BACKEND_PORT: String(activePort), LOCALSTACK_CONFIG_PATH: path.join(confDir, 'config.toml') },
      stdio: ['ignore', beLog, beLog]
    })
    sidecarChild.on('error', () => { /* health 轮询会兜底判定失败 */ })
    if (sidecarChild.pid) ownedPids.add(sidecarChild.pid)
    backendStarted = true
    try { fs.closeSync(beLog) } catch { /* child 已持句柄 */ }
    const healthy = await waitHealthy(activeApiBase + '/')
    if (!healthy) return { ok: false, stage: 'health', apiBase: activeApiBase }
    // owner_verified（评审 FCV 末梢 + localstack.sh 既有纪律）：健康文案可被
    // 冒名——监听者必须是我们 spawn 的子进程，防「以他人健康冒充成功」。
    const owner = portListenerPids(activePort)
    if (!sidecarChild || !sidecarChild.pid || !owner.includes(String(sidecarChild.pid))) {
      return { ok: false, stage: 'owner', apiBase: activeApiBase, out: `listener=${owner.join(',') || 'none'} mine=${sidecarChild && sidecarChild.pid}` }
    }
    return { ok: true, stage: 'ready', apiBase: activeApiBase }
  }

  // SIGTERM → 等待退出 → SIGKILL → 二次确认；返回是否确认退出
  async function killAndWait(child, timeoutMs = 8000) {
    if (!child) return true
    if (child.exitCode !== null || child.signalCode !== null) return true // 已退出（避免空等 8s）
    return await new Promise((resolve) => {
      let settled = false
      const finish = (v) => { if (!settled) { settled = true; clearTimeout(t); resolve(v) } }
      const t = setTimeout(() => {
        try { child.kill('SIGKILL') } catch { finish(true); return }
        setTimeout(() => finish(child.exitCode !== null || child.signalCode !== null), 1500)
      }, timeoutMs)
      child.once('exit', () => finish(true))
      try { child.kill('SIGTERM') } catch { finish(true) }
    })
  }

  function portListenerPids(port) {
    const r = spawnSync('lsof', ['-nP', `-iTCP:${port}`, '-sTCP:LISTEN', '-t'], { env: process.env })
    const out = (r.stdout || '').toString().trim()
    return out ? out.split(/\s+/) : []
  }

  function rememberPostmasterPid() {
    try {
      const raw = fs.readFileSync(path.join(home, 'pgdata', 'postmaster.pid'), 'utf8').split('\n')[0].trim()
      if (/^\d+$/.test(raw)) ownedPids.add(Number(raw))
    } catch { /* no pid file */ }
  }

  // 最终兜底：仅回收「本栈 spawn 过」的 pid（含 postmaster）；外来进程一律不动
  function reapOwnedListeners(port, logger) {
    rememberPostmasterPid()
    for (const pid of portListenerPids(port)) {
      const n = Number(pid)
      if (!ownedPids.has(n)) {
        logger.log(`[local-stack] WARN: port ${port} held by pid ${n} (not ours) — left untouched`)
        continue
      }
      try { process.kill(n, 'SIGKILL'); logger.log(`[local-stack] reaped owned pid ${n} on :${port}`) } catch { /* gone */ }
    }
  }

  async function sidecarStop() {
    const sidecar = resolveSidecar()
    const backendPort = activePort

    if (sidecarChild) {
      const ok = await killAndWait(sidecarChild)
      logger.log(`[local-stack] backend stopped=${ok}`)
      sidecarChild = null
      backendStarted = false
    }
    reapOwnedListeners(backendPort, logger)   // 先回收后端端口（避免被 PG 兜底的 30s 阻塞拖延）

    if (pgChild || pgStarted) {
      const ok = await killAndWait(pgChild)
      logger.log(`[local-stack] pg supervisor stopped=${ok}`)
      pgChild = null
      if (portListenerPids(pgPort).length !== 0) {
        const stateFile = path.join(home, 'pg_state.json')
        if (sidecar) spawnSync(sidecar, ['pg', 'stop', '--state-file', stateFile], { env: baseEnv(), timeout: 30000 })
        logger.log('[local-stack] fallback pg stop issued')
      }
      reapOwnedListeners(pgPort, logger)
      pgStarted = false
    }

    // 复核（一次重试）
    await new Promise((r) => setTimeout(r, 400))
    if (portListenerPids(pgPort).length) reapOwnedListeners(pgPort, logger)
    if (portListenerPids(backendPort).length) reapOwnedListeners(backendPort, logger)
  }

  // ---- 脚本（开发形态） ----
  async function scriptStart() {
    if (isLocalBase) {
      const choice = await chooseBackendPort(activePort, logger)
      activePort = choice.port
      activeApiBase = `http://127.0.0.1:${activePort}`
    }
    logger.log(`[local-stack] starting (pg:local backend:${activePort})`)
    // script 模式 PG 归 localstack.sh 管（测试栈 PG 独立于 app 的 cfg.pgPort）；
    // 只透传后端端口，并把 [server].port 回写到该端口（评审 medium：漂移会让
    // self-eval 打到旧端口/外来进程）
    patchConfigPorts(resolveServerDir(), activePort, null, logger)
    const scriptEnv = { LOCALSTACK_BACKEND_PORT: String(activePort) }
    const pg = await runScript('localstack', ['start'], { env: scriptEnv })
    logger.log(`[local-stack] pg: exit=${pg.code}\n${pg.out.trim().split('\n').slice(-2).join('\n')}`)
    if (pg.code !== 0) return { ok: false, stage: 'pg', out: pg.out }
    pgStarted = true

    const be = await runScript('localstack', ['backend-start'], { env: scriptEnv })
    logger.log(`[local-stack] backend: exit=${be.code}\n${be.out.trim().split('\n').slice(-2).join('\n')}`)
    if (be.code !== 0) return { ok: false, stage: 'backend', out: be.out }
    backendStarted = true

    const healthy = await waitHealthy(activeApiBase + '/')
    if (!healthy) return { ok: false, stage: 'health', apiBase: activeApiBase }
    logger.log(`[local-stack] health(${activeApiBase}/): OK`)
    return { ok: true, stage: 'ready', apiBase: activeApiBase }
  }

  async function scriptStop() {
    const be = await runScript('localstack', ['backend-stop'])
    logger.log(`[local-stack] backend-stop (exit=${be.code}): ${be.out.trim().split('\n').pop()}`)
    backendStarted = false
    const pg = await runScript('localstack', ['stop'])
    logger.log(`[local-stack] pg-stop (exit=${pg.code}): ${pg.out.trim().split('\n').pop()}`)
    pgStarted = false
  }

  const sidecar = resolveSidecar()

  async function start() {
    if (!isLocalBase) {
      // 外部后端模式（apiBase 指向非本机）：本地栈整体跳过（不 spawn PG/后端）
      logger.log(`[local-stack] skipped (external apiBase: ${configuredApiBase})`)
      return { ok: true, stage: 'skipped', apiBase: configuredApiBase, external: true }
    }
    if (sidecar) {
      logger.log(`[local-stack] sidecar mode: ${sidecar}`)
      const r = await sidecarStart(sidecar)
      if (r.ok) logger.log(`[local-stack] health(${activeApiBase}/): OK`)
      return r
    }
    return scriptStart()
  }

  // 逐资源跟踪：部分失败也能清理（避免退出后遗留 PG/后端）
  async function stop() {
    if (!pgChild && !pgStarted && !backendStarted) return
    if (sidecar) return sidecarStop()
    return scriptStop()
  }

  return {
    start, stop, config: cfg, mode: sidecar ? 'sidecar' : 'scripts',
    apiBase: activeApiBase,
    getApiBase: () => activeApiBase,
    getBackendPort: () => activePort
  }
}

module.exports = { createLocalStack, loadLocalConfig, waitHealthy, runScript, isPortFree, chooseBackendPort }
