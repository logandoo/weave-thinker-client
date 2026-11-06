/* W26 §B3 插话 echo 配对规则（行为级单测）。
 *
 * Run: node --experimental-strip-types tests/workflows/test_interjection_pairing.mjs
 */
import { pairInterjectionEcho } from '../../src/stores/interjectionPairing.ts';

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

// 1. 同文两条：id 精确配对，不回落内容
{
  const pend = [
    { content: '同一句话', clientMessageId: 'cm-1' },
    { content: '同一句话', clientMessageId: 'cm-2' },
  ];
  check('P1 同文两条按 id 精确配对第二条',
    pairInterjectionEcho(pend, { content: '同一句话', client_message_id: 'cm-2' }) === 1);
  check('P2 有 id 但不匹配 → -1（绝不回落内容）',
    pairInterjectionEcho(pend, { content: '同一句话', client_message_id: 'cm-404' }) === -1);
  check('P3 空 id 字符串按无 id 处理（内容兜底取最旧未确认）',
    pairInterjectionEcho(pend, { content: '同一句话', client_message_id: '' }) === 0);
}

// 2. 旧后端 live echo（无 id）→ 内容兜底
{
  const pend = [
    { content: '旧文本', clientMessageId: 'cm-old', committed: true },
    { content: '旧文本', clientMessageId: 'cm-new' },
  ];
  check('P4 live 无 id：优先未确认条目（跳过已 committed）',
    pairInterjectionEcho(pend, { content: '旧文本' }) === 1);
}

// 3. replay 复放的无 id echo 不得确认带 cmid 的新条目
{
  const pend = [{ content: '重复文本', clientMessageId: 'cm-new' }];
  check('P5 replay 无 id：带 cmid 的条目不受旧 echo 影响',
    pairInterjectionEcho(pend, { content: '重复文本' }, { fromReplay: true }) === -1);
  const legacy = [{ content: '重复文本' }];
  check('P6 replay 无 id：无 cmid 的旧条目可确认',
    pairInterjectionEcho(legacy, { content: '重复文本' }, { fromReplay: true }) === 0);
}

// 4. 未知回声 / 空内容
{
  check('P7 未知内容 → -1', pairInterjectionEcho([{ content: 'a' }], { content: 'b' }) === -1);
  check('P8 空内容无 id → -1', pairInterjectionEcho([{ content: 'a' }], {}) === -1);
  check('P9 空队列 → -1', pairInterjectionEcho([], { content: 'a' }) === -1);
}

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
