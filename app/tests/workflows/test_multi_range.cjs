/* 笔记波 2026-10-25 —— 虚拟多 range（不连续选择）纯决策逻辑（§A4.8 测试先行；RED 先行）。
 *
 * 钉住四组纯函数（不含 DOM）：
 *  1. expandWordRun：ctrl+单击的词段展开（拉丁词段 / CJK 单字 / 空白标点 null）；
 *  2. commitSpan：段提交（全等 toggle 移除、重叠合并、排序、非法丢弃）；
 *  3. dropSpanCovering：点命中已有段 → 整段移除（Google Docs 取消语义）；
 *  4. shouldHealComposingFromInput / FromKey：IME 组合标志陈旧自愈判定。
 *
 * 行为级证据由 e2e/note_multirange_toolbar_renumber_20261025.spec.ts 承担。
 * Run: node --experimental-strip-types tests/workflows/test_multi_range.cjs
 */
const path = require('node:path');

async function main() {
  const mod = await import(
    path.resolve(__dirname, '../../src/composables/multiRange.ts')
  );
  const {
    expandWordRun, commitSpan, dropSpanCovering, filterValidSpans,
    shouldHealComposingFromInput, shouldHealComposingFromKey,
  } = mod;

  let pass = 0;
  let fail = 0;
  const check = (name, cond, detail) => {
    if (cond) { pass++; console.log(`PASS ${name}`); }
    else { fail++; console.log(`FAIL ${name}${detail === undefined ? '' : ' | ' + JSON.stringify(detail)}`); }
  };
  const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  // ── 1. expandWordRun ─────────────────────────────────────────────
  check('拉丁词中部 → 整词', eq(expandWordRun('azsxdcfvgb', 3), { start: 0, end: 10 }));
  check('词中夹数字下划线同属词段', eq(expandWordRun('ab_12 cd', 2), { start: 0, end: 5 }));
  check('词尾边界（caret=词末）取左词', eq(expandWordRun('az dc', 2), { start: 0, end: 2 }));
  check('词首边界取右词', eq(expandWordRun('az dc', 3), { start: 3, end: 5 }));
  check('CJK 单字', eq(expandWordRun('地球力量', 2), { start: 2, end: 3 }));
  check('空白 → null', expandWordRun('az  dc', 3) === null);
  check('标点两侧均非词字符 → null', expandWordRun('az, dc', 3) === null);
  check('空文本 → null', expandWordRun('', 0) === null);
  check('caret 越界钳制', eq(expandWordRun('az', 99), { start: 0, end: 2 }));
  check('拉丁带变音字母算词', eq(expandWordRun('aé b', 1), { start: 0, end: 2 }));

  // ── 2. commitSpan ────────────────────────────────────────────────
  check('空表新增', eq(commitSpan([], { start: 2, end: 4 }), [{ start: 2, end: 4 }]));
  check('全等 toggle 移除', eq(commitSpan([{ start: 2, end: 4 }], { start: 2, end: 4 }), []));
  check('不同段并存且排序', eq(commitSpan([{ start: 8, end: 9 }], { start: 2, end: 4 }), [{ start: 2, end: 4 }, { start: 8, end: 9 }]));
  check('重叠合并（相交）', eq(commitSpan([{ start: 2, end: 6 }], { start: 4, end: 8 }), [{ start: 2, end: 8 }]));
  check('相邻合并（首尾相接）', eq(commitSpan([{ start: 2, end: 5 }], { start: 5, end: 8 }), [{ start: 2, end: 8 }]));
  check('包含合并', eq(commitSpan([{ start: 5, end: 6 }], { start: 1, end: 9 }), [{ start: 1, end: 9 }]));
  check('非法段丢弃（start>=end）', eq(commitSpan([], { start: 4, end: 4 }), []));
  check('负起点钳到 0', eq(commitSpan([], { start: -3, end: 2 }), [{ start: 0, end: 2 }]));

  // ── 3. dropSpanCovering ──────────────────────────────────────────
  {
    const r = dropSpanCovering([{ start: 2, end: 6 }, { start: 9, end: 12 }], 4);
    check('命中移除该段', r.removed === true && eq(r.spans, [{ start: 9, end: 12 }]));
  }
  {
    const r = dropSpanCovering([{ start: 2, end: 6 }], 7);
    check('未命中不移除', r.removed === false && eq(r.spans, [{ start: 2, end: 6 }]));
  }
  {
    const r = dropSpanCovering([{ start: 2, end: 6 }], 6);
    check('右开端点不算命中', r.removed === false);
  }

  // ── 4. filterValidSpans ──────────────────────────────────────────
  check('越界钳制与空段剔除', eq(filterValidSpans([{ start: 0, end: 5 }, { start: 8, end: 20 }, { start: 3, end: 3 }], 10), [{ start: 0, end: 5 }, { start: 8, end: 10 }]));
  check('文档清空全剔除', eq(filterValidSpans([{ start: 0, end: 5 }], 0), []));

  // ── 5. shouldHealComposingFromInput / FromKey ────────────────────
  check('普通插入且浏览器报非组合 → 自愈', shouldHealComposingFromInput('insertText', false) === true);
  check('真组合中的 input（isComposing=true）→ 不自愈', shouldHealComposingFromInput('insertText', true) === false);
  check('insertCompositionText → 永不自愈', shouldHealComposingFromInput('insertCompositionText', false) === false);
  check('deleteCompositionText → 不自愈', shouldHealComposingFromInput('deleteCompositionText', false) === false);
  check('insertFromComposition → 不自愈', shouldHealComposingFromInput('insertFromComposition', false) === false);
  check('isComposingProp 缺失（旧浏览器）→ 不自愈（保守）', shouldHealComposingFromInput('insertText', undefined) === false);
  check('可打印键且非组合 → 自愈', shouldHealComposingFromKey('a', false) === true);
  check('组合期按键（isComposing=true）→ 不自愈', shouldHealComposingFromKey('a', true) === false);
  check('修饰键（Shift）→ 不自愈', shouldHealComposingFromKey('Shift', false) === false);
  check('Process 键 → 不自愈', shouldHealComposingFromKey('Process', false) === false);
  check('isComposingProp 缺失 → 不自愈（保守）', shouldHealComposingFromKey('a', undefined) === false);

  console.log(`\nRESULT: ${fail === 0 ? 'ALL CHECKS PASS' : fail + ' FAILED'} (${pass} pass / ${fail} fail)`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error('IMPORT/RUN FAIL:', e.message); process.exit(1); });
