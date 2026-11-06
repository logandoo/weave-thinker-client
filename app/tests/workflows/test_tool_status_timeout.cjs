/**
 * 工具步骤状态推导 —— 超时载荷必须显示为「失败」（2026-11-01 审计 F4）
 * Run: cd frontend && node --experimental-strip-types tests/workflows/test_tool_status_timeout.cjs
 *
 * 红线：同一调用在「流式中」与「刷新回放后」状态必须一致；持久化步骤没有
 * status 字段，不得再一律当作 completed。
 */
const { deriveToolStatus, payloadIsTimeout } = require('../../src/utils/toolStatus.ts')

const cases = []
const t = (name, fn) => cases.push([name, fn])

const TIMEOUT_PAYLOAD = JSON.stringify({
  stdout: '',
  stderr: '',
  return_code: -1,
  error: 'Command timed out after 120.0s',
  timed_out: true,
  killed_by: 'SIGKILL',
  timeout_effective: 120,
  guidance: '…',
})

t('F4-1 回放：timed_out 载荷（无 status）→ error', () => {
  const s = deriveToolStatus({ content: TIMEOUT_PAYLOAD })
  if (s !== 'error') throw new Error(`期望 error，实际 ${s}`)
})

t('F4-2 回放：普通成功载荷 → completed', () => {
  const s = deriveToolStatus({ content: '{"stdout":"ok\\n","return_code":0}' })
  if (s !== 'completed') throw new Error(`期望 completed，实际 ${s}`)
})

t('F4-3 实时：显式 status 优先于载荷推断', () => {
  if (deriveToolStatus({ status: 'running', content: TIMEOUT_PAYLOAD }) !== 'running') {
    throw new Error('running 必须保留')
  }
  if (deriveToolStatus({ status: 'completed', content: '{"a":1}' }) !== 'completed') {
    throw new Error('completed 必须保留')
  }
})

t('F4-3b 形状收窄：缺 return_code=-1 的 JSON 不算超时', () => {
  if (payloadIsTimeout('{"timed_out": true}')) {
    throw new Error('缺少 return_code=-1 不得判为超时（避免误伤用户读到的 JSON 文件）')
  }
})

t('F4-4 非 JSON / 正文里提到 timed_out 不得误判', () => {
  if (payloadIsTimeout('这个脚本里写了 "timed_out": true 的字段')) {
    throw new Error('非 JSON 文本不得判为超时')
  }
  if (payloadIsTimeout('{"timed_out": false}')) {
    throw new Error('timed_out:false 不得判为超时')
  }
  if (deriveToolStatus({ content: '普通文本结果' }) !== 'completed') {
    throw new Error('普通文本应 completed')
  }
})

t('F4-5 载荷已是对象（未序列化）也要识别', () => {
  if (!payloadIsTimeout({ timed_out: true, return_code: -1 })) throw new Error('对象载荷未识别')
})

t('F4-6 空/异常输入不得抛错', () => {
  for (const v of [null, undefined, '', '{坏 JSON']) {
    const s = deriveToolStatus({ content: v })
    if (s !== 'completed') throw new Error(`输入 ${String(v)} 应 completed，实际 ${s}`)
  }
})

let failed = 0
for (const [name, fn] of cases) {
  try { fn(); console.log(`PASS ${name}`) } catch (e) { failed += 1; console.log(`FAIL ${name} | ${e.message}`) }
}
console.log(`RESULT: ${cases.length - failed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
