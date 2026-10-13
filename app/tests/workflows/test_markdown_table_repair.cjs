/* Markdown table repair tests (2026-09-15).
 * Regression for production conv dbcb5269: the assistant message emitted a GFM
 * table WITHOUT the delimiter row → marked rendered raw pipes as plain text.
 * Run: node tests/workflows/test_markdown_table_repair.cjs */
let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/composables/markdownTableRepair.ts');
  const { fixMissingTableSeparators, fixMarkdownTables } = mod;

  // ── A: production shape — header without delimiter, CJK cells ──────────
  const broken = [
    '关键时间线如下：',
    '',
    '| 时间 | 事件 |',
    '| 8月15日 | Buckmaster 与 Alpöge 得到受迫欧拉等三项光滑外力爆破结果 |',
    '| 8月22日 | 两人完成 Lean 机器验证 |',
    '| 9月11日 | 25 位菲尔兹奖得主发布联名宣言 |',
    '',
    '值得注意的是……',
  ].join('\n');
  const repaired = fixMissingTableSeparators(broken);
  const lines = repaired.split('\n');
  check('A1 separator inserted after header',
    lines[2] === '| 时间 | 事件 |' && lines[3] === '|---|---|', lines.slice(0, 6));
  check('A1b body rows untouched',
    lines[4] === '| 8月15日 | Buckmaster 与 Alpöge 得到受迫欧拉等三项光滑外力爆破结果 |' &&
    lines[6] === '| 9月11日 | 25 位菲尔兹奖得主发布联名宣言 |', lines.slice(4, 8));
  check('A1c prose untouched',
    lines[0] === '关键时间线如下：' && lines[8] === '值得注意的是……', lines);

  // ── B: no-op cases ─────────────────────────────────────────────────────
  const valid = '| H | V |\n| --- | --- |\n| a | 1 |\n';
  check('B1 valid table unchanged', fixMissingTableSeparators(valid) === valid);
  const mismatched = '| a | b |\n|---|\n| 1 | 2 |\n';
  check('B2 existing (mismatched) separator not duplicated',
    fixMissingTableSeparators(mismatched) === mismatched);
  const lone = '| x |\nnot a table\n';
  check('B3 lone pipe line untouched', fixMissingTableSeparators(lone) === lone);
  const single = '| x |\n| y |\n';
  check('B4 single-cell pair untouched (conservative)', fixMissingTableSeparators(single) === single);
  const nopipes = 'no tables here\n';
  check('B5 no-pipe text unchanged', fixMissingTableSeparators(nopipes) === nopipes);

  // ── C: fenced code is code ─────────────────────────────────────────────
  const fenced = '```text\n| a | b |\n| c | d |\n```\n';
  check('C1 fenced code untouched', fixMissingTableSeparators(fenced) === fenced);
  const tilde = '~~~md\n| a | b |\n| c | d |\n~~~\n';
  check('C2 tilde fence untouched', fixMissingTableSeparators(tilde) === tilde);
  const fenceThenTable = '```\n| a | b |\n```\n\n| x | y |\n| 1 | 2 |\n';
  check('C3 post-fence table repaired, fence intact',
    fixMissingTableSeparators(fenceThenTable) === '```\n| a | b |\n```\n\n| x | y |\n|---|---|\n| 1 | 2 |\n',
    fixMissingTableSeparators(fenceThenTable));

  // ── D: counting / structure ────────────────────────────────────────────
  const three = '| a | b | c |\n| 1 | 2 | 3 |\n';
  check('D1 three-column header → three-column separator',
    fixMissingTableSeparators(three) === '| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n');
  const escaped = '| a \\| b | c |\n| 1 | 2 |\n';
  check('D2 escaped pipe not overcounted',
    fixMissingTableSeparators(escaped) === '| a \\| b | c |\n|---|---|\n| 1 | 2 |\n',
    fixMissingTableSeparators(escaped));
  const afterHeading = '## 二、前因\n| 时间 | 事件 |\n| 8月15日 | A |\n';
  check('D3 repairs table right after a heading line',
    fixMissingTableSeparators(afterHeading) === '## 二、前因\n| 时间 | 事件 |\n|---|---|\n| 8月15日 | A |\n',
    fixMissingTableSeparators(afterHeading));
  check('D4 idempotent', fixMissingTableSeparators(repaired) === repaired);

  // ── E: composed pipeline (repair → column-count repair) ────────────────
  // Escaped pipes (`\|`) are one cell in GFM — the column-count stage must
  // not "repair" the correct 2-column delimiter back to 3 columns.
  const escBroken = '| a \\| b | c |\n| 1 | 2 |\n';
  const escComposed = fixMarkdownTables(fixMissingTableSeparators(escBroken));
  check('E1 composed: escaped-pipe header keeps 2-column separator',
    escComposed === '| a \\| b | c |\n|---|---|\n| 1 | 2 |\n', escComposed);
  const escWithSep = '| a \\| b | c |\n|---|---|\n| 1 | 2 |\n';
  check('E2 escaped-pipe header with correct separator unchanged',
    fixMarkdownTables(escWithSep) === escWithSep, fixMarkdownTables(escWithSep));
  const mismatched2 = '| a | b |\n|---|\n| 1 | 2 |\n';
  check('E3 column-count repair still fixes mismatched separators',
    fixMarkdownTables(mismatched2) === '| a | b |\n|---|---|\n| 1 | 2 |\n', fixMarkdownTables(mismatched2));

  console.log(failed === 0 ? '\nall checks pass' : `\n${failed} check(s) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
