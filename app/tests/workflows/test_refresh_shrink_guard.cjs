/**
 * Refresh shrink-guard (conv 4e159a79, 2026-10-01): the just-finalized
 * answer's server row was a strict SUFFIX of the displayed content (persisted
 * row lost its HEAD at a tool-call boundary) — refresh replaced the displayed
 * content and the user watched blocks 1-6 vanish ("被替换删除").
 *
 * Contract: keepLongerDisplayed keeps the LOCAL content only when the server
 * content is a strict suffix of it AND the lost head is ≥ MIN_LOST_HEAD_CHARS
 * (real prose). Legitimate trims still apply:
 *   - citation sanitize strips [N] mid-text      → divergent → server wins
 *   - orphan colon/punct strip (≤2 leading chars) → below threshold → server wins
 *   - equal content / server longer / empty local → server wins
 *
 * Run: cd frontend && node --experimental-strip-types tests/workflows/test_refresh_shrink_guard.cjs
 */
const { keepLongerDisplayed, MIN_LOST_HEAD_CHARS } = require('../../src/stores/streamReducer.ts')

const cases = []
function t(name, fn) { cases.push([name, fn]) }

// 1. incident shape: DB lost the head, server row = suffix of displayed
t('suffix head-loss keeps local', () => {
  const local = '一、二、三、四、五、六块正文……逐渐适应的## 七、势力组织（续）'
  const server = local.slice(local.indexOf('的## 七'))
  if (server !== '的## 七、势力组织（续）') throw new Error('setup broken')
  const kept = keepLongerDisplayed(local, server)
  if (kept !== local) throw new Error(`expected local kept, got ${JSON.stringify(kept)}`)
})

// 2. equal → server
t('equal content applies server', () => {
  if (keepLongerDisplayed('same', 'same') !== 'same') throw new Error('fail')
})

// 3. server longer (e.g. low-confidence note appended) → server
t('server longer applies server', () => {
  if (keepLongerDisplayed('answer', 'answer\n\n（低置信说明）') !== 'answer\n\n（低置信说明）') throw new Error('fail')
})

// 4. empty local → server
t('empty local applies server', () => {
  if (keepLongerDisplayed('', 'server text') !== 'server text') throw new Error('fail')
  if (keepLongerDisplayed(null, 'server text') !== 'server text') throw new Error('fail')
})

// 5. citation sanitize (mid-text [N] removal) → divergent, server wins
t('citation sanitize shape applies server', () => {
  const local = '依据[3]的研究与[7]的综述，结论成立。'
  const server = '依据的研究与的综述，结论成立。'
  if (keepLongerDisplayed(local, server) !== server) throw new Error('fail')
})

// 6. orphan colon strip (1 leading char) → below threshold, server wins
t('punctuation-level trim applies server', () => {
  const local = '：结论如下。'
  const server = '结论如下。'
  if (local.length - server.length >= MIN_LOST_HEAD_CHARS) throw new Error('setup broken')
  if (keepLongerDisplayed(local, server) !== server) throw new Error('fail')
})

// 7. divergent head (not a suffix) → server wins
t('divergent content applies server', () => {
  if (keepLongerDisplayed('完全不同的本地内容', '服务端内容') !== '服务端内容') throw new Error('fail')
})

// 8. threshold boundary: exactly MIN_LOST_HEAD_CHARS lost → keep local
t('threshold boundary keeps local', () => {
  const server = '尾部内容'
  const local = 'ab' + server  // 丢头 2 字 < MIN_LOST_HEAD_CHARS(3)
  if (local.length - server.length !== MIN_LOST_HEAD_CHARS - 1) throw new Error('setup broken')
  if (keepLongerDisplayed(local, server) !== server) throw new Error('below threshold must apply server')
  const local2 = 'abc' + server  // 丢头 3 字 = 阈值 → 保留本地
  if (keepLongerDisplayed(local2, server) !== local2) throw new Error('at threshold must keep local')
})

let failed = 0
for (const [name, fn] of cases) {
  try { fn(); console.log(`PASS ${name}`) } catch (e) { failed++; console.error(`FAIL ${name}: ${e.message}`) }
}
if (failed) { console.error(`${failed} FAILED`); process.exit(1) }
console.log(`ALL ${cases.length} PASS`)
