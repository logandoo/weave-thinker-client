// 渲染缓存键回归（C6）：旧键 len+head4+tail4 同长同头尾碰撞串稿。
// 三重断言：①新键属性（同串同键/异串异键，含构造碰撞对）②useMarkdown 已接线
// ③旧方案在该碰撞对上确实相等（反例存档，证明测试有牙齿）。
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const { stableRenderCacheKey } = await import(
    path.resolve(__dirname, '../../src/composables/stringCacheKey.ts')
  );

  let pass = 0;
  let fail = 0;
  const check = (cond, name) => {
    if (cond) { pass++; console.log(`PASS ${name}`); }
    else { fail++; console.log(`FAIL ${name}`); }
  };

  // 构造碰撞对：同长度、同 head4、同 tail4、中段不同
  const A = '甲'.padEnd(20, '一') + 'AAA中段差异内容AAA' + '乙'.padEnd(20, '二');
  const B = '甲'.padEnd(20, '一') + 'BBB完全不同内容BBB' + '乙'.padEnd(20, '二');
  const oldKey = (s) => (s.length <= 8 ? s : `${s.length}:${s.slice(0, 4)}:${s.slice(-4)}`);

  check(A.length === B.length, 'collision pair same length');
  check(oldKey(A) === oldKey(B), 'legacy scheme collides on the pair (counterexample archived)');
  check(stableRenderCacheKey(A) !== stableRenderCacheKey(B), 'new key distinguishes collision pair');
  check(stableRenderCacheKey(A) === stableRenderCacheKey(A), 'new key stable for identical content');
  check(stableRenderCacheKey('') === stableRenderCacheKey(''), 'new key handles empty');
  check(stableRenderCacheKey('abc') !== stableRenderCacheKey('abd'), 'new key distinguishes tiny diffs');
  check(stableRenderCacheKey('x'.repeat(10)) !== stableRenderCacheKey('x'.repeat(11)), 'new key distinguishes length');

  // 接线断言：useMarkdown 必须使用 stableRenderCacheKey 且不得回退旧拼接键
  const src = fs.readFileSync(path.resolve(__dirname, '../../src/composables/useMarkdown.ts'), 'utf8');
  check(src.includes("from './stringCacheKey'"), 'useMarkdown imports stableRenderCacheKey');
  check(src.includes('stableRenderCacheKey('), 'useMarkdown calls stableRenderCacheKey');
  check(!src.includes('${len}:${head}:${tail}'), 'legacy colliding key removed');

  console.log(`RESULT: ${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
