/* 笔记遗留 #2 —— 选区/光标恢复的纯决策逻辑（§A4.8 测试先行；RED 先行）。
 *
 * 本测试钉住一件事（不含 DOM，纯函数）：
 *  1. 重渲染后是否恢复选区：四门禁（快照非空 ∧ 文本相同 ∧ 编辑器持焦点 ∧ 非 IME 组合期）；
 *
 * 行为级证据由 e2e/note_caret_restore.spec.ts（8158 生产构建）承担。
 * Run: node --experimental-strip-types tests/workflows/test_selection_restore.cjs
 */
const path = require('node:path');

async function main() {
  const mod = await import(
    path.resolve(__dirname, '../../src/composables/selectionRestore.ts')
  );
  const { shouldRestoreAfterRender } = mod;

  let pass = 0;
  let fail = 0;
  const check = (name, cond, detail) => {
    if (cond) { pass++; console.log(`PASS ${name}`); }
    else { fail++; console.log(`FAIL ${name}${detail === undefined ? '' : ' | ' + JSON.stringify(detail)}`); }
  };

  const snap = (startOffset, endOffset, text) => ({ startOffset, endOffset, text });

  // ── 1. 重渲染恢复门禁 ────────────────────────────────────────────────
  check('空快照不恢复', shouldRestoreAfterRender(null, '甲乙', true, false) === false);
  check('普通情况恢复（同文本+焦点+非组合）',
    shouldRestoreAfterRender(snap(0, 2, '甲乙丙丁'), '甲乙丙丁', true, false) === true);
  check('文本改变不恢复（外部插入/切笔记）',
    shouldRestoreAfterRender(snap(0, 2, '甲乙丙丁'), '甲乙丙丁X', true, false) === false);
  check('编辑器未持焦点不恢复',
    shouldRestoreAfterRender(snap(0, 2, '甲乙丙丁'), '甲乙丙丁', false, false) === false);
  check('IME 组合期不恢复',
    shouldRestoreAfterRender(snap(0, 2, '甲乙丙丁'), '甲乙丙丁', true, true) === false);

  console.log(`RESULT: ${fail === 0 ? 'ALL CHECKS PASS' : fail + ' CHECKS FAILED'} (${pass} pass / ${fail} fail)`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error('ERROR', e && e.message ? e.message : e); process.exit(1); });
