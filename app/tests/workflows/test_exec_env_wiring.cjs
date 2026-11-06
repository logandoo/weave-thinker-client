/* W18 G5 执行环境 UI 接线不变量（A4.9 Critical 钉）。
 *
 * 背景：A4.9 r1 Critical——onMounted 里 addEventListener('click', onOutsideClick)
 * 引用未定义函数（唯一定义是 handleOutsideClick），mount 即 ReferenceError，
 * 把 loadModels / G7 偏好恢复 / 草稿回填全数杀死，而 vite build（无类型检查）
 * 与 test:unit 都不会红。
 *
 * 契约：
 *  1. onMounted 注册的 click 监听器名在文件内有定义（防自由变量）；
 *  2. onUnmounted 移除的与注册的是同一个函数；
 *  3. 发送前校验是 async 且强制刷新设备列表（不依赖菜单打开态）；
 *  4. 偏好恢复带 execUserSelected 竞态护栏；
 *  5. selectExecTarget 持久化默认执行环境（saveUiPreferences）。
 *
 * Run: node tests/workflows/test_exec_env_wiring.cjs */
const fs = require('fs')
const path = require('path')

let failed = 0
let passed = 0
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`)
  if (!cond) failed += 1
  else passed += 1
}

const SRC = path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatInput.vue')
const src = fs.readFileSync(SRC, 'utf8')

// 1. onMounted 块内 addEventListener 引用的名字必须在文件内有 function/const 定义
const mountedBlock = src.match(/onMounted\(\(\) => \{[\s\S]*?\n\}\)/)
check('onMounted block found', !!mountedBlock)
if (mountedBlock) {
  const listeners = [...mountedBlock[0].matchAll(/addEventListener\('click',\s*([A-Za-z_$][\w$]*)\)/g)]
  check('click listener registered', listeners.length >= 1)
  for (const m of listeners) {
    const name = m[1]
    const defined =
      src.includes(`function ${name}(`) ||
      src.includes(`const ${name} =`) ||
      src.includes(`const ${name}=`)
    check(`listener ${name} defined in file`, defined, name)
  }
}

// 2. onUnmounted 移除与注册同名
const unmountedBlock = src.match(/onUnmounted\(\(\) => \{[\s\S]*?\n\}\)/)
check('onUnmounted block found', !!unmountedBlock)
if (mountedBlock && unmountedBlock) {
  const added = [...mountedBlock[0].matchAll(/addEventListener\('click',\s*([A-Za-z_$][\w$]*)\)/g)].map(m => m[1])
  const removed = [...unmountedBlock[0].matchAll(/removeEventListener\('click',\s*([A-Za-z_$][\w$]*)\)/g)].map(m => m[1])
  check('click add/remove symmetric', added.length > 0 && added.every(n => removed.includes(n)), { added, removed })
}

// 3. 发送前校验 async + 强制刷新
check('execPreSendCheck is async', /async function execPreSendCheck/.test(src))
check('pre-send refreshes device list', /execPreSendCheck[\s\S]{0,1600}getCloudDevices\(\)/.test(src))  // W32：联动面=云端注册表（test-change 判据 #100）
check('handleSend awaits pre-check', /if \(!\(await execPreSendCheck\(\)\)\) return/.test(src))

// 4. 偏好恢复竞态护栏
check('restore race guard present', /execUserSelected\.value/.test(src) && /!execUserSelected\.value/.test(src))
check('selection marks user intent', /execUserSelected\.value = true/.test(src))

// 5. 持久化
check('selection persists preference', /selectExecTarget[\s\S]{0,900}saveUiPreferences\(\{ default_execution_target: target \?\? '' \}\)/.test(src))

// PLAN T2/T3/T4：绑定落库、三态解析与默认起点（只增断言，不改既有 W18 合同）
const STORE_SRC = path.resolve(__dirname, '..', '..', 'src', 'stores', 'chat.ts')
const API_SRC = path.resolve(__dirname, '..', '..', 'src', 'api', 'chat.ts')
const SIDEBAR_SRC = path.resolve(__dirname, '..', '..', 'src', 'components', 'Sidebar.vue')
const ZEN_SRC = path.resolve(__dirname, '..', '..', 'src', 'components', 'ZenHistoryDrawer.vue')
const store = fs.readFileSync(STORE_SRC, 'utf8')
const api = fs.readFileSync(API_SRC, 'utf8')
const sidebar = fs.readFileSync(SIDEBAR_SRC, 'utf8')
const zen = fs.readFileSync(ZEN_SRC, 'utf8')

check('resolver uses pure three-state helper', /resolveExecutionTarget\(/.test(store) && /utils\/executionTarget/.test(store))
const hadMessagesIndex = store.indexOf('const hadMessages =')
const optimisticPushIndex = store.indexOf('messages.value[conversationId].push(userMessage)', hadMessagesIndex)
check('first send captures pre-optimistic-message snapshot', hadMessagesIndex > 0 && optimisticPushIndex > hadMessagesIndex)
check('first send passes message snapshot to resolver', /resolveExecTargetFor\([^\n]+hadMessages\)/.test(store))

check('chat API exposes execution-target POST binding', /bindExecutionTarget[\s\S]{0,500}execution-target/.test(api))
check('menu selection is async', /async function selectExecTarget/.test(src))
check('menu selection awaits shared store binding', /selectExecTarget[\s\S]{0,1800}await chatStore\.bindExecutionTarget/.test(src))
check('binding pending state exists', /execBindingBusy/.test(src))
check('pre-send blocks while binding is pending', /execPreSendCheck[\s\S]{0,500}execBindingBusy\.value/.test(src))
check('failed selection restores previous label', /selectExecTarget[\s\S]{0,2600}previousTarget/.test(src))
check('store binding synchronizes row meta', /bindExecutionTarget[\s\S]{0,1800}conversationMeta\.value\[convId\]/.test(store))
check('store binding synchronizes sidebar row', /bindExecutionTarget[\s\S]{0,2200}conversations\.value/.test(store))

check('create resolves preference default instead of residual target', /createConversation[\s\S]{0,900}resolveDefaultExecutionTarget/.test(store) && !/chatApi\.createConversation\(title, assistantId, executionTarget\.value\)/.test(store))
check('unavailable default produces explicit warning', /默认设备不可用/.test(store))
check('shared empty-row default reconciliation exists', /applyDefaultExecutionTargetToConversation/.test(store))
check('Sidebar empty reuse applies default after select', /selectConversation\(emptyConv\.id\)[\s\S]{0,300}applyDefaultExecutionTargetToConversation\(emptyConv\.id\)/.test(sidebar))
check('Zen empty reuse applies default after select', /selectConversation\(emptyConv\.id\)[\s\S]{0,300}applyDefaultExecutionTargetToConversation\(emptyConv\.id\)/.test(zen))

// A4.9 R1 — committed-response loss / stale reads / explicit intent boundaries
check('ambiguous binding outcome reconciles with server', /reconcileExecutionTarget/.test(src) && /execBindingUncertain/.test(src))
check('stale reads guarded by execution-target version', /_executionTargetVersions/.test(store))
check('refresh applies global target only for non-stale response', /refreshConversation[\s\S]{0,2200}_executionTargetVersions/.test(store))
check('auto-create honors explicit preselection', /executionTargetUserSelected/.test(store))
check('Sidebar reuse checks loaded message count', /messages\[emptyConv\.id\]\?\.length/.test(sidebar))
check('Zen reuse checks loaded message count', /messages\[emptyConv\.id\]\?\.length/.test(zen))
check('restore rejection also preserves explicit selection', /catch\(\(\) => \{[\s\S]{0,350}!execUserSelected\.value/.test(src))
check('same target selection is an early no-op', /previousTarget === target/.test(src))
check('pre-send resolves target from row/meta authority', /execPreSendCheck[\s\S]{0,500}resolveExecTargetFor/.test(src))
check('reconciliation advances authoritative version', /reconcileExecutionTarget[\s\S]{0,900}advanceExecutionTargetVersion\(_executionTargetVersions, convId\)/.test(store))
check('refresh target applies only latest request', /isLatestRefresh/.test(store))
check('select target applies only latest current selection', /isLatestSelect/.test(store) && /id === currentConversationId\.value/.test(store))
check('explicit preselection is scoped to no-conversation flow', /executionTargetUserSelected = !convId/.test(src))

// Shared request-order domain: a newer read/reconcile/bind invalidates older
// work at REQUEST time, never by response-arrival order.
for (const name of ['refreshConversation', 'selectConversation', 'reconcileExecutionTarget']) {
  const start = store.indexOf(`async function ${name}(`)
  const next = store.indexOf('\n  async function ', start + 1)
  const body = start >= 0 ? store.slice(start, next > start ? next : undefined) : ''
  const advanceAt = body.indexOf('advanceExecutionTargetVersion')
  const getAt = body.indexOf('chatApi.getConversation')
  const guardAt = body.indexOf('isExecutionTargetVersionCurrent')
  const advanceCount = (body.match(/advanceExecutionTargetVersion\(/g) || []).length
  check(
    `${name} allocates request version before GET and guards response`,
    advanceAt >= 0 && getAt > advanceAt && guardAt > getAt && advanceCount === 1,
    { advanceAt, getAt, guardAt, advanceCount },
  )
  if (name === 'reconcileExecutionTarget') {
    check(
      'superseded reconcile fails closed instead of returning stale meta',
      /if \(!isExecutionTargetVersionCurrent\([\s\S]{0,240}throw new Error/.test(body),
    )
  }
}
const selectStart = store.indexOf('async function selectConversation(')
const selectBody = store.slice(selectStart, store.indexOf('\n  async function ', selectStart + 1))
check(
  'same-conversation select no-op does not invalidate reads',
  selectBody.indexOf('if (currentConversationId.value === id) return') >= 0
    && selectBody.indexOf('if (currentConversationId.value === id) return') < selectBody.indexOf('advanceExecutionTargetVersion')
    && selectBody.indexOf('if (currentConversationId.value === id) return') < selectBody.indexOf('_selectConversationSeqs[id] = selectSeqAtStart'),
)
const bindStart = store.indexOf('async function bindExecutionTarget(')
const bindBody = store.slice(bindStart, store.indexOf('\n  async function ', bindStart + 1))
check(
  'successful bind advances shared version before applying result',
  bindBody.indexOf('await chatApi.bindExecutionTarget') >= 0
    && bindBody.indexOf('advanceExecutionTargetVersion') > bindBody.indexOf('await chatApi.bindExecutionTarget')
    && bindBody.indexOf('advanceExecutionTargetVersion') < bindBody.indexOf('conversationMeta.value[convId] ='),
)
check(
  'only accepted select and refresh responses synchronize sidebar rows',
  /if \(isLatestRefresh && !targetChangedWhileLoading\) \{\s*conv\.execution_target = conversationMeta\.value\[conversationId\]\?\.execution_target \?\? null\s*\}/.test(store)
    && /if \(isLatestSelect && !targetChangedWhileLoading\) \{\s*conv\.execution_target = conversationMeta\.value\[id\]\?\.execution_target \?\? null\s*\}/.test(store),
)
check(
  'select-inserted sidebar row preserves undefined and null meta states',
  /execution_target: conversationMeta\.value\[id\]\?\.execution_target,/.test(store)
    && !/execution_target: conversationMeta\.value\[id\]\?\.execution_target \?\? null/.test(store),
)

console.log(`\n=== ${passed} pass / ${failed} fail ===`)
process.exit(failed ? 1 : 0)
