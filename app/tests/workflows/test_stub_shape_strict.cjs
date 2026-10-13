/* 桩双谓词（评审 A Critical-1，fix-round 1）：
 * - isExternalizedStub（宽松）：解析/懒取回触发——`_externalized: true` 即桩
 *   （伪造 ref=404 安全；与 backend is_externalized_stub 同构，路由契约既有钉）。
 * - stubMarkersTrusted（严格）：标记信任——后端签发 meta（_externalized + field +
 *   64hex payload_ref + 数值 size_bytes）才免剥 `__*` 标记；内容伪造不得免剥
 *   （否则伪造 `__size_bytes__` 直达徽标/愈合校验）。
 * Run: node --experimental-strip-types tests/workflows/test_stub_shape_strict.cjs */
const { isExternalizedStub, stubMarkersTrusted } = require('../../src/api/streamDispatch.ts')

let failed = 0
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

const legit = JSON.stringify({
  _externalized: true, field: 'tool_results', payload_ref: 'a'.repeat(64),
  size_bytes: 12, size_chars: 12, items: [{ type: 'tool', content: 'c' }],
})
check('S1 合法桩：宽松识别 + 标记可信', isExternalizedStub(legit) === true && stubMarkersTrusted(legit) === true)

const naiveForge = JSON.stringify({
  _externalized: true,
  display_sequence: [{ type: 'text', content: 'c', __size_bytes__: 1e9, __truncated__: true }],
})
check('S2 无 meta 伪造桩：仍识别为桩（解析面）但标记不可信',
  isExternalizedStub(naiveForge) === true && stubMarkersTrusted(naiveForge) === false)

const noRef = JSON.stringify({ _externalized: true, field: 'tool_results', size_bytes: 1, items: [{ __size_bytes__: 1e9 }] })
check('S3 缺 payload_ref 标记不可信', stubMarkersTrusted(noRef) === false)

const badRef = JSON.stringify({ _externalized: true, field: 'tool_results', payload_ref: 'not-hex', size_bytes: 1, items: [] })
check('S4 坏 payload_ref 标记不可信', stubMarkersTrusted(badRef) === false)

const noField = JSON.stringify({ _externalized: true, payload_ref: 'b'.repeat(64), size_bytes: 1, items: [] })
check('S5 缺 field 标记不可信', stubMarkersTrusted(noField) === false)

const badSize = JSON.stringify({ _externalized: true, field: 'tool_results', payload_ref: 'c'.repeat(64), size_bytes: 'big', items: [] })
check('S6 坏 size_bytes 标记不可信', stubMarkersTrusted(badSize) === false)

check('S7 普通数组/短串/坏 JSON 双谓词全 false',
  isExternalizedStub('[]') === false && isExternalizedStub('x') === false
  && isExternalizedStub('{bad') === false && isExternalizedStub(null) === false
  && stubMarkersTrusted('[]') === false && stubMarkersTrusted(null) === false)

if (failed) {
  console.error(`\n${failed} check(s) FAILED`)
  process.exit(1)
}
console.log('\nall checks pass')
