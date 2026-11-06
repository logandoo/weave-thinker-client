/* 插话内联展示纯函数（2026-11-01）。
 *
 * 覆盖：持久化 display_sequence 缺失/存在两种形态、after_index 插入、
 * 重复 message_id 去重回放、content 按 after_char 回退切段、
 * 顶层 user 插话行过滤（不漏掉真实用户消息）。
 *
 * Run: node --experimental-strip-types tests/workflows/test_interjection_inline_display.mjs
 */
import {
  mergeInterjectionItems,
  resolvePersistedInterjectionSequence,
  fallbackSequenceFromContent,
  filterEmbeddedInterjections,
  applyLiveInterjection,
} from '../../src/stores/interjectionDisplay.ts';

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}
function eq(a, b, name) { check(name, JSON.stringify(a) === JSON.stringify(b), { got: a, want: b }); }

const item = (id, after_index, after_char, content = '插话') => ({
  type: 'interjection', message_id: id, content, created_at: '2026-11-01T11:22:47',
  after_index, after_char,
});

// 1. Persisted sequence missing items: insert at after_index, keep order.
{
  const seq = [
    { type: 'reasoning_step', content: 'r' },
    { type: 'text', content: 'before' },
    { type: 'tool', call_id: 'c1' },
  ];
  const out = resolvePersistedInterjectionSequence(seq, [
    item('ij-1', 2, 6),
  ], '');
  eq(out.map(x => x.type), ['reasoning_step', 'text', 'interjection', 'tool'], 'P1 after_index insert order');
  eq(out[2].message_id, 'ij-1', 'P1 inserted id');
}

// 2. Sequence already contains the item → no duplicate (replay parity).
{
  const seq = [{ type: 'text', content: 'a' }, item('ij-1', 1, 1)];
  const out = resolvePersistedInterjectionSequence(seq, [item('ij-1', 1, 1)], '');
  eq(out.filter(x => x.type === 'interjection').length, 1, 'P2 no duplicate interjection');
}

// 3. after_index clamping (bad/negative/too-large values).
{
  const seq = [{ type: 'text', content: 'a' }];
  eq(mergeInterjectionItems(seq, [item('ij-1', 99, 1)])[1].message_id, 'ij-1', 'P3 large index clamps to tail');
  eq(mergeInterjectionItems(seq, [item('ij-2', -3, 1)])[0].message_id, 'ij-2', 'P3 negative index clamps to head');
}

// 4. No display_sequence (payload stub): split content at after_char.
{
  const content = 'before\n\nAFTER';
  const out = fallbackSequenceFromContent(content, [
    item('ij-1', 0, 6),
    item('ij-2', 0, 13),
  ]);
  eq(out.map(x => x.type), ['text', 'interjection', 'text', 'interjection'], 'P4 fallback split shape');
  eq(out[0].content, 'before', 'P4 first text');
  eq(out[2].content, '\n\nAFTER', 'P4 tail text');
}

// 5. Fallback with out-of-range after_char must not throw or lose content.
{
  const out = fallbackSequenceFromContent('abc', [item('ij-1', 0, 99)]);
  eq(out.map(x => x.type), ['text', 'interjection'], 'P5 out-of-range offset keeps text');
  eq(out[0].content, 'abc', 'P5 text preserved');
}

// 6. Top-level user interjection rows are hidden only when embedded.
{
  const messages = [
    { id: 'u1', role: 'user', content: '原始 query' },
    { id: 'a1', role: 'assistant', content: 'answer', interjection_items: [item('ij-1', 1, 0)] },
    { id: 'ij-1', role: 'user', content: '插话', embedded_in_message_id: 'a1' },
    { id: 'u2', role: 'user', content: '下一轮 query' },
  ];
  const visible = filterEmbeddedInterjections(messages);
  eq(visible.map(m => m.id), ['u1', 'a1', 'u2'], 'P6 only embedded interjection hidden');
}

// 6b. The in-flight parent is not in the loaded messages yet: hide only when
// the replay's streaming row id matches.
{
  const messages = [
    { id: 'u1', role: 'user', content: '原始 query' },
    { id: 'ij-live', role: 'user', content: '插话', embedded_in_message_id: 'stream-1' },
  ];
  eq(filterEmbeddedInterjections(messages, 'stream-1').map(m => m.id), ['u1'], 'P6b live parent hides embedded row');
  eq(filterEmbeddedInterjections(messages, 'other').map(m => m.id), ['u1', 'ij-live'], 'P6b other live id keeps row');
}

// 7. Live commit appends once; duplicate echo is idempotent.
{
  const seq = [{ type: 'text', content: 'streaming' }];
  const once = applyLiveInterjection(seq, item('ij-1', 1, 0));
  const twice = applyLiveInterjection(once, item('ij-1', 1, 0));
  eq(twice.map(x => x.type), ['text', 'interjection'], 'P7 live append once');
}

// 8. Empty interjection list is a no-op (legacy conversations).
{
  const seq = [{ type: 'text', content: 'legacy' }];
  eq(resolvePersistedInterjectionSequence(seq, [], 'body'), seq, 'P8 legacy no-op');
  eq(filterEmbeddedInterjections([{ id: 'u1', role: 'user' }]).length, 1, 'P8 legacy user row kept');
}

console.log(`RESULT: ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
