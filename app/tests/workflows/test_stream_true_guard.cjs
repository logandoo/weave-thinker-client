/**
 * Stream "true" guard (conv 827a6f78, 2026-09-03): the live streaming view
 * once degraded to the literal text "true" — any non-string slipping into a
 * text channel is concatenated by the accumulators (`'' + true === "true"`),
 * while the persisted DB answer stays clean. Regression contract:
 * dispatchStreamPayload drops non-string text channels at the single choke
 * point, so no boolean/null ever reaches an accumulator.
 *
 * Run: node --experimental-strip-types tests/workflows/test_stream_true_guard.cjs
 */
const { dispatchStreamPayload } = require('../../src/api/streamDispatch.ts')

function makeState() {
  const s = { content: '', reasoning: '', partMode: false, displaySequence: [], subAgentChunks: {}, toolResults: [] }
  const partIdx = new Map()
  const handlers = {
    onMessage: (chunk) => { s.content += chunk },
    onReasoning: (chunk) => { s.reasoning += chunk },
    onContentSegment: (seg) => { if (seg.trim()) s.displaySequence.push({ type: 'text', content: seg }) },
    onPartStarted: (part) => { s.partMode = true; if (!partIdx.has(part.part_id)) { const item = { type: part.part_type || 'text', part_id: part.part_id, content: part.content ?? '' }; s.displaySequence.push(item); partIdx.set(part.part_id, item) } },
    onPartDelta: (part) => { s.partMode = true; let item = partIdx.get(part.part_id); if (!item) { handlers.onPartStarted({ part_id: part.part_id, part_type: 'text' }); item = partIdx.get(part.part_id) } const f = part.field || 'content'; item[f] = (item[f] || '') + part.delta },
    onSubAgentChunk: (c) => { const b = s.subAgentChunks[c.subtask_id] || { content: '', reasoning: '' }; if (c.kind === 'content') b.content += c.delta; s.subAgentChunks[c.subtask_id] = b },
    onToolResult: (tr) => { s.toolResults.push(tr) },
    onError: (e) => { s.error = e },
  }
  return { s, handlers }
}

const cases = []
// 1. boolean content must NOT append
{
  const { s, handlers } = makeState()
  s.content = '前文'
  dispatchStreamPayload({ content: true }, handlers)
  cases.push(['boolean content dropped', s.content === '前文'])
}
// 2. null content must NOT append "null"
{
  const { s, handlers } = makeState()
  s.content = '前文'
  dispatchStreamPayload({ content: null }, handlers)
  cases.push(['null content dropped', s.content === '前文'])
}
// 3. string content still flows
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ content: '正常' }, handlers)
  cases.push(['string content flows', s.content === '正常'])
}
// 4. boolean reasoning dropped
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ reasoning_content: true }, handlers)
  cases.push(['boolean reasoning dropped', s.reasoning === ''])
}
// 5. part_delta boolean delta → no item synthesized, no "true" rendered
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ part_delta: { part_id: 'p1', part_type: 'text', field: 'content', delta: true } }, handlers)
  const rendered = s.displaySequence.map(i => String(i.content)).join('')
  cases.push(['boolean part_delta dropped', !rendered.includes('true') && s.displaySequence.length === 0])
}
// 6. part_delta string delta still appends
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ part_delta: { part_id: 'p1', part_type: 'text', field: 'content', delta: '文本' } }, handlers)
  cases.push(['string part_delta appends', s.displaySequence[0]?.content === '文本'])
}
// 7. sub_agent_chunk boolean delta dropped
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ sub_agent_chunk: { subtask_id: 't1', subtask_name: 'n', kind: 'content', delta: true } }, handlers)
  cases.push(['boolean sub_agent delta dropped', (s.subAgentChunks.t1?.content || '') === ''])
}
// 8. segment boolean dropped
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ segment_content: false }, handlers)
  cases.push(['boolean segment dropped', s.displaySequence.length === 0])
}
// 9. error non-string coerced to a safe string
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ error: true }, handlers)
  cases.push(['non-string error coerced', typeof s.error === 'string' && s.error.length > 0])
}
// 10. failed tool_result (gate-blocked workspace write) routes to onToolResult —
//     it is a per-tool failure, NOT a stream-fatal error (2026-10-09: the fatal
//     branch swallowed it as 未知错误 + killed the stream + wiped the bubble)
{
  const { s, handlers } = makeState()
  s.content = '前文'
  const gateResult = JSON.stringify({ error: 'GATE-BLOCKED (vibeweaver physical gate): WRITE SUCCEEDED', gate: 'after-hook' })
  const ret = dispatchStreamPayload({ call_id: 'c1', name: 'workspace_write', result: gateResult, error: true }, handlers)
  const tr = s.toolResults[0]
  cases.push(['failed tool_result routes to onToolResult',
    ret === false && s.toolResults.length === 1 && s.error === undefined
    && tr.error === true && tr.result === gateResult && tr.name === 'workspace_write'])
}
// 11. successful tool_result (error:false) still routes to onToolResult
{
  const { s, handlers } = makeState()
  const ret = dispatchStreamPayload({ call_id: 'c2', name: 'workspace_write', result: '{"ok":true}', error: false }, handlers)
  cases.push(['ok tool_result still routes', ret === false && s.toolResults.length === 1 && s.toolResults[0].error === false && s.error === undefined])
}
// 12. stream stays usable after a failed tool_result (no termination)
{
  const { s, handlers } = makeState()
  dispatchStreamPayload({ call_id: 'c3', name: 'workspace_write', result: '{}', error: true }, handlers)
  const ret = dispatchStreamPayload({ content: '后续正文' }, handlers)
  cases.push(['stream continues after failed tool_result', ret === false && s.content === '后续正文' && s.error === undefined])
}
// 13. genuine stream-level error frame (string) still fatal
{
  const { s, handlers } = makeState()
  const ret = dispatchStreamPayload({ error: '流级错误' }, handlers)
  cases.push(['stream error frame still fatal', ret === true && s.error === '流级错误' && s.toolResults.length === 0])
}

let fails = 0
for (const [name, ok] of cases) {
  if (!ok) fails += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`)
}
console.log(fails === 0 ? '\nall checks pass' : `\n${fails} FAILURES`)
process.exit(fails === 0 ? 0 : 1)
