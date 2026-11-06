/**
 * Wave 2 — task result routing invariants (pure helpers).
 *
 * Run: node --experimental-strip-types tests/workflows/test_voice_delegation_ui.cjs
 */
const {
  resolveTaskConversationId,
  describeTaskState,
} = require('../../src/utils/taskResult.ts')

let passed = 0
let failed = 0
function check(name, cond, detail = '') {
  if (cond) {
    passed += 1
    console.log(`PASS ${name}`)
  } else {
    failed += 1
    console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

check(
  'execution conversation wins for running and completed tasks',
  resolveTaskConversationId({
    execution_conversation_id: 'c2',
    output_conversation_id: 'c2-old',
  }) === 'c2',
)
check(
  'output conversation is the fallback for legacy rows',
  resolveTaskConversationId({
    execution_conversation_id: null,
    output_conversation_id: 'c2',
  }) === 'c2',
)
check(
  'no conversation yields null (never a guessed C1)',
  resolveTaskConversationId({
    execution_conversation_id: null,
    output_conversation_id: null,
  }) === null,
)
check(
  'waiting states are not described as failures',
  describeTaskState('waiting_auth').includes('授权') &&
    !describeTaskState('waiting_auth').includes('失败'),
)
check(
  'completed keeps the "not verified goal" caveat',
  describeTaskState('completed').includes('不等于目标已验证达成'),
)
check(
  'timed_out discloses partial results',
  describeTaskState('timed_out').includes('部分结果'),
)

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed === 0 ? 0 : 1)
