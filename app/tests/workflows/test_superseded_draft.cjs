/**
 * P0 R2 (conv 08249cf0): parked pre-tool prose is a draft/process unit.
 * - collapse flag must survive replay merge;
 * - audit_reset draft surgery must NOT delete already-shown superseded text.
 * Run: node --experimental-strip-types tests/workflows/test_superseded_draft.cjs
 */
const fs = require('fs')
const path = require('path')
const { dropDraftTextAfterLastTool, mergeReplayIntoSequence, markPreToolDrafts } = require('../../src/stores/streamReducer.ts')

const cases = []
const t = (name, fn) => cases.push([name, fn])

t('superseded text after last tool is kept on reset', () => {
  const seq = [
    { type: 'text', content: 'parked draft', superseded: true },
    { type: 'tool_call', call_id: 'c1' },
    { type: 'text', content: 'rejected draft' },
  ]
  const { kept, changed } = dropDraftTextAfterLastTool(seq)
  if (!changed) throw new Error('expected rejected draft to be dropped')
  if (!kept.some(i => i.content === 'parked draft')) throw new Error('superseded draft vanished')
  if (kept.some(i => i.content === 'rejected draft')) throw new Error('rejected draft survived')
})

t('superseded text is kept even with no tool card', () => {
  const seq = [
    { type: 'text', content: 'keep me', superseded: true },
    { type: 'text', content: 'drop me' },
  ]
  const { kept } = dropDraftTextAfterLastTool(seq)
  if (!kept.some(i => i.content === 'keep me')) throw new Error('superseded draft vanished')
  if (kept.some(i => i.content === 'drop me')) throw new Error('plain draft survived')
})

t('replay merge carries superseded flag onto live item', () => {
  const live = [{ type: 'text', content: 'draft', part_id: 'p1' }]
  const replay = [{ type: 'text', content: 'draft', part_id: 'p1', superseded: true }]
  const out = mergeReplayIntoSequence(live, replay)
  if (out[0].superseded !== true) throw new Error('superseded flag lost in replay merge')
})

t('legacy replay duplicate merges superseded instead of suppressing it', () => {
  const live = [{ type: 'text', content: 'draft' }]
  const replay = [{ type: 'text', content: 'draft', superseded: true }]
  const out = mergeReplayIntoSequence(live, replay)
  if (out.length !== 1) throw new Error(`duplicate item: ${out.length}`)
  if (out[0].superseded !== true) throw new Error('legacy replay flag lost')
})

t('partMode live item + legacy replay parked item does not duplicate', () => {
  const live = [{ type: 'text', content: 'draft', part_id: 'p1' }]
  const replay = [{ type: 'text', content: 'draft', superseded: true }]
  const out = mergeReplayIntoSequence(live, replay)
  if (out.length !== 1) throw new Error(`duplicate item: ${out.length}`)
  if (out[0].superseded !== true) throw new Error('flag not merged onto partMode item')
})

t('new tool card marks prior streamed text as draft', () => {
  const seq = [
    { type: 'text', content: 'pre-tool draft' },
    { type: 'tool_call', call_id: 'c1' },
    { type: 'text', content: 'final answer' },
  ]
  const changed = markPreToolDrafts(seq)
  if (changed !== 1) throw new Error(`expected 1 mark, got ${changed}`)
  if (seq[0].superseded !== true) throw new Error('pre-tool text not marked')
  if (seq[2].superseded) throw new Error('post-tool answer wrongly marked')
})

t('chat.ts wires draft marking into both live tool paths', () => {
  const src = fs.readFileSync(path.join(__dirname, '../../src/stores/chat.ts'), 'utf8')
  if (!src.includes('markPreToolDrafts(s.displaySequence)')) throw new Error('onToolCall/applyPartStarted wiring missing')
  const hits = (src.match(/markPreToolDrafts\(s\.displaySequence\)/g) || []).length
  if (hits < 2) throw new Error(`expected >=2 wiring sites, got ${hits}`)
})

let failed = 0
for (const [name, fn] of cases) {
  try { fn(); console.log(`PASS ${name}`) } catch (e) { failed++; console.error(`FAIL ${name}: ${e.message}`) }
}
console.log(`\nRESULT: ${cases.length - failed} passed, ${failed} failed`)
if (failed) process.exit(1)
console.log('RESULT: ALL CHECKS PASS')
