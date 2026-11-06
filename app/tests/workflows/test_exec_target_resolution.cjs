/* PLAN T2 — execution target 三态行为测试（node --experimental-strip-types）。 */
const { pathToFileURL } = require('url')
const path = require('path')
const fs = require('fs')

let passed = 0
let failed = 0
function check(name, actual, expected) {
  const ok = Object.is(actual, expected)
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : ` | expected=${JSON.stringify(expected)} actual=${JSON.stringify(actual)}`}`)
  if (ok) passed += 1
  else failed += 1
}

async function main() {
  let resolveExecutionTarget
  let advanceExecutionTargetVersion
  let isExecutionTargetVersionCurrent
  try {
    const modulePath = path.resolve(__dirname, '..', '..', 'src', 'utils', 'executionTarget.ts')
    ;({
      resolveExecutionTarget,
      advanceExecutionTargetVersion,
      isExecutionTargetVersionCurrent,
      isExecutionTargetRejection,
    } = await import(pathToFileURL(modulePath).href))
  } catch (error) {
    console.log(`FAIL pure resolver module available | ${error.message}`)
    failed += 1
  }
  if (typeof resolveExecutionTarget !== 'function') {
    console.log(`\n=== ${passed} pass / ${failed} fail ===`)
    process.exit(1)
  }

  const base = {
    conversationId: 'conv-current',
    currentConversationId: 'conv-current',
    storeTarget: 'device:store-device',
    hasMessages: false,
  }

  check(
    'undefined meta + empty current conversation falls back to store',
    resolveExecutionTarget({ ...base, metaTarget: undefined }),
    'device:store-device',
  )
  check(
    'defined null meta wins over store',
    resolveExecutionTarget({ ...base, metaTarget: null }),
    null,
  )
  check(
    'message snapshot prevents store override',
    resolveExecutionTarget({ ...base, metaTarget: undefined, hasMessages: true }),
    null,
  )
  check(
    'defined device meta remains authoritative with messages',
    resolveExecutionTarget({ ...base, metaTarget: 'device:row-device', hasMessages: true }),
    'device:row-device',
  )
  check(
    'non-current empty conversation never leaks active store target',
    resolveExecutionTarget({ ...base, currentConversationId: 'conv-other', metaTarget: undefined }),
    null,
  )
  check(
    'no conversation id keeps preselection for auto-create',
    resolveExecutionTarget({ ...base, conversationId: null, metaTarget: undefined }),
    'device:store-device',
  )

  const versionHelpersReady = typeof advanceExecutionTargetVersion === 'function'
    && typeof isExecutionTargetVersionCurrent === 'function'
  const versionCases = [
    'select slow then refresh fast accepts only newer refresh',
    'refresh slow then select fast accepts only newer select',
    'ambiguous-bind reconcile invalidates older read',
    'newest reconcile remains applicable',
    'successful bind invalidates earlier reconcile',
    'successful bind token is current',
    'target versions are isolated by conversation',
  ]
  if (!versionHelpersReady) {
    for (const name of versionCases) check(name, 'helpers missing', 'ready')
  } else {
    const readOrder = {}
    const selectToken = advanceExecutionTargetVersion(readOrder, 'conv')
    const refreshToken = advanceExecutionTargetVersion(readOrder, 'conv')
    const selectThenRefresh = []
    for (const [name, token] of [['select', selectToken], ['refresh', refreshToken]]) {
      if (isExecutionTargetVersionCurrent(readOrder, 'conv', token)) selectThenRefresh.push(name)
    }
    check(versionCases[0], selectThenRefresh.join(','), 'refresh')

    const reverseOrder = {}
    const olderRefreshToken = advanceExecutionTargetVersion(reverseOrder, 'conv')
    const newerSelectToken = advanceExecutionTargetVersion(reverseOrder, 'conv')
    const refreshThenSelect = []
    for (const [name, token] of [['refresh', olderRefreshToken], ['select', newerSelectToken]]) {
      if (isExecutionTargetVersionCurrent(reverseOrder, 'conv', token)) refreshThenSelect.push(name)
    }
    check(versionCases[1], refreshThenSelect.join(','), 'select')

    const recovery = {}
    const oldReadToken = advanceExecutionTargetVersion(recovery, 'conv')
    // An ambiguous bind failure starts reconciliation without first advancing
    // the sequence; the newer reconcile must supersede every older read.
    const reconcileToken = advanceExecutionTargetVersion(recovery, 'conv')
    check(versionCases[2], isExecutionTargetVersionCurrent(recovery, 'conv', oldReadToken), false)
    check(versionCases[3], isExecutionTargetVersionCurrent(recovery, 'conv', reconcileToken), true)
    const successfulBindToken = advanceExecutionTargetVersion(recovery, 'conv')
    check(versionCases[4], isExecutionTargetVersionCurrent(recovery, 'conv', reconcileToken), false)
    check(versionCases[5], isExecutionTargetVersionCurrent(recovery, 'conv', successfulBindToken), true)
    advanceExecutionTargetVersion(recovery, 'other-conv')
    check(versionCases[6], isExecutionTargetVersionCurrent(recovery, 'conv', successfulBindToken), true)
  }

  if (typeof isExecutionTargetRejection === 'function') {
    check('target rejection 400 device-unbound', isExecutionTargetRejection({
      response: { status: 400, data: { detail: '设备不存在或已解绑' } },
    }), true)
    check('target rejection 422 execution_target shape', isExecutionTargetRejection({
      response: { status: 422, data: { detail: 'execution_target 须为 device:<id> 或留空' } },
    }), true)
    check('target rejection 400 other business error', isExecutionTargetRejection({
      response: { status: 400, data: { detail: '会话标题过长' } },
    }), false)
    check('target rejection 500 device text is not fallback', isExecutionTargetRejection({
      response: { status: 500, data: { detail: '设备不存在或已解绑' } },
    }), false)
    check('target rejection null error', isExecutionTargetRejection(null), false)

    const chatSrc = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf8')
    check('createConversation retries null target on device rejection',
      /isExecutionTargetRejection\(e\)[\s\S]{0,500}createConversation\(title, assistantId, null\)/.test(chatSrc), true)
    const sidebarSrc = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'src', 'components', 'Sidebar.vue'), 'utf8')
    const zenSrc = fs.readFileSync(
      path.resolve(__dirname, '..', '..', 'src', 'components', 'ZenHistoryDrawer.vue'), 'utf8')
    check('Sidebar new-chat navigates even if target bind fails',
      !sidebarSrc.includes('if (!(await chatStore.applyDefaultExecutionTargetToConversation'), true)
    check('ZenHistoryDrawer new-chat opens even if target bind fails',
      !zenSrc.includes('if (!(await chatStore.applyDefaultExecutionTargetToConversation'), true)
  } else {
    for (const name of [
      'target rejection 400 device-unbound',
      'target rejection 422 execution_target shape',
      'target rejection 400 other business error',
      'target rejection 500 device text is not fallback',
      'target rejection null error',
      'createConversation retries null target on device rejection',
      'Sidebar new-chat navigates even if target bind fails',
      'ZenHistoryDrawer new-chat opens even if target bind fails',
    ]) check(name, 'helper missing', 'ready')
  }

  console.log(`\n=== ${passed} pass / ${failed} fail ===`)
  process.exit(failed ? 1 : 0)
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
