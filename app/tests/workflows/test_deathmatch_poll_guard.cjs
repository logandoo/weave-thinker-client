// B-6 poll 竞态守卫（deathmatchPollGuard.ts）：纯语义 + chat.ts 接线 pin。
// 跑法: node --experimental-strip-types tests/workflows/test_deathmatch_poll_guard.cjs
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const { shouldApplyPollVerdict } = await import(
    path.resolve(__dirname, '../../src/stores/deathmatchPollGuard.ts')
  );

  let pass = 0;
  let fail = 0;
  const check = (cond, name) => {
    if (cond) { pass++; console.log(`PASS ${name}`); }
    else { fail++; console.log(`FAIL ${name}`); }
  };

  // 纯语义：无并发 → 应用；期间 bump → 弃用
  check(shouldApplyPollVerdict(0, 0) === true, 'no concurrent verdict applies');
  check(shouldApplyPollVerdict(3, 3) === true, 'stable seq applies');
  check(shouldApplyPollVerdict(3, 4) === false, 'seq advanced during fetch → drop stale snapshot');
  check(shouldApplyPollVerdict(0, 2) === false, 'multiple verdicts landed → drop');

  // 接线 pin：chat.ts 必须（a）onDeathmatchVerdict 递增序号（b）GET 前取快照
  // （c）响应后过 shouldApplyPollVerdict（否则守卫=死代码）
  const src = fs.readFileSync(
    path.resolve(__dirname, '../../src/stores/chat.ts'), 'utf8');
  check(/shouldApplyPollVerdict/.test(src), 'chat.ts wires shouldApplyPollVerdict');
  check(/_verdictSeq/.test(src), 'chat.ts keeps a verdict seq counter');
  const guardIdx = src.indexOf('shouldApplyPollVerdict(');  // 调用点，非 import 行
  const fetchIdx = src.indexOf('getConversation(convId, { include:');
  check(guardIdx > fetchIdx, 'guard is checked after the poll GET');

  // I6（fix-round）：所有 deathmatchVerdict 写点都递增 _verdictSeq——
  // 写点 bump 数 ≥ verdict 赋值数（裸写点漏 bump = poll 竞态守卫被旁路）。
  const assigns = (src.match(/deathmatchVerdict\s*=/g) || []).length;
  const bumps = (src.match(/_verdictSeq\.set\(/g) || []).length;
  check(assigns >= 1, `chat.ts still has verdict assignments (found ${assigns})`);
  check(bumps >= assigns,
    `verdict write-point bumps (${bumps}) >= deathmatchVerdict assignments (${assigns})`);

  console.log(`\n=== ${pass} pass / ${fail} fail ===`);
  if (fail > 0) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
