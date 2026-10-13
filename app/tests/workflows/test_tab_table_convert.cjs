/* Tab-separated table conversion tests (conv a104fbc5, 2026-09-20).
 * Regression: a user pasting a table (browser/Excel clipboard = tabs) got a
 * run-on blob in the bubble; user messages now convert tab blocks to GFM
 * tables before markdown rendering.
 * Run: node tests/workflows/test_tab_table_convert.cjs */
let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/composables/tabTableConvert.ts');
  const { convertTabTablesToMarkdown } = mod;

  // ── A: production shape — the conv a104fbc5 paste (header + data rows) ──
  const pasted = [
    '表6-2 基准回归设定与执行状态（样本区间：2018—2025年，重点2022—2025年）',
    '',
    '命题\t被解释变量\t核心解释变量\t估计方法/固定效应\t数据来源\t执行状态',
    'P1 传导阻滞\t企业活期存款占比、M1—M2增速差\t货币乘数、信用脉冲\t时序局部投影；趋势项控制\t中国人民银行\t受限估计：不填报数值',
    'P2 配置偏向\t中小微融资占比（近似）\t银行风险偏好指标\t省际面板；双向固定效应\t银行家问卷调查\t受限估计：不填报数值',
  ].join('\n');
  const converted = convertTabTablesToMarkdown(pasted);
  const cLines = converted.split('\n');
  check('A1 header row becomes pipe row with 6 columns',
    cLines[2] === '| 命题 | 被解释变量 | 核心解释变量 | 估计方法/固定效应 | 数据来源 | 执行状态 |', cLines[2]);
  check('A2 separator synthesized',
    cLines[3] === '| --- | --- | --- | --- | --- | --- |', cLines[3]);
  check('A3 data rows converted (no tabs left)',
    !converted.includes('\t') && cLines[4].startsWith('| P1 传导阻滞 |'), cLines[4]);
  check('A4 row count preserved (2 data rows)',
    cLines.filter((l) => l.startsWith('| P')).length === 2);
  check('A5 prose around table untouched',
    cLines[0] === '表6-2 基准回归设定与执行状态（样本区间：2018—2025年，重点2022—2025年）' &&
    cLines[1] === '', cLines.slice(0, 2));

  // ── B: conservative no-ops ──────────────────────────────────────────────
  const single = '公式：y\ti = α\t+ βX';
  check('B1 single tabbed line untouched', convertTabTablesToMarkdown(single) === single);
  const prose = '你好，帮我看看这个\n没有制表符的消息。';
  check('B2 tab-free text unchanged', convertTabTablesToMarkdown(prose) === prose);
  const oneTabLine = 'a\tb\nc d';
  check('B3 <2 tabs per line untouched', convertTabTablesToMarkdown(oneTabLine) === oneTabLine);

  // ── C: fenced code is opaque ───────────────────────────────────────────
  const fenced = '```python\nx\t=\t1\nprint(x)\ny\t=\t2\n```';
  check('C1 fenced code untouched', convertTabTablesToMarkdown(fenced) === fenced);
  const afterFence = '```\ncode\there\n```\n\na\tb\tc\nd\te\tf';
  const af = convertTabTablesToMarkdown(afterFence);
  check('C2 table after fence converted, fence intact',
    af.startsWith('```\ncode\there\n```') && af.includes('| a | b | c |'), af);

  // ── D: cell escaping ───────────────────────────────────────────────────
  const pipes = 'a\tb|c\td\ne\tf\tg';
  const pd = convertTabTablesToMarkdown(pipes);
  check('D1 pipe inside cell escaped',
    pd.split('\n')[0] === '| a | b\\|c | d |', pd.split('\n')[0]);

  // ── E: idempotence ─────────────────────────────────────────────────────
  check('E1 idempotent', convertTabTablesToMarkdown(converted) === converted);

  // ── F: multiple blocks ─────────────────────────────────────────────────
  const two = 'h1\ta\tb\nr1\tc\td\n\nh2\te\tf\nr2\tg\th';
  const tw = convertTabTablesToMarkdown(two);
  check('F1 two tables split by blank line',
    tw.split('\n').filter((l) => l.startsWith('| ---')).length === 2, tw);

  // ── G: ragged rows padded; <2-tab lines end the block (conservative) ──
  const ragged = 'a\tb\tc\nd\te\t\nf\tg\th';
  const rg = convertTabTablesToMarkdown(ragged).split('\n');
  check('G1 trailing-empty cell row padded to 3 columns',
    rg[2] === '| d | e |  |' || rg[2] === '| d | e | |', rg[2]);
  check('G2 all three rows in the table', rg.length === 4, rg);
  const splitBySingleTab = 'a\tb\tc\nx y\tz\nf\tg\th';
  const sb = convertTabTablesToMarkdown(splitBySingleTab).split('\n');
  check('G3 1-tab middle line ends the block (no conversion)',
    sb.length === 3 && !sb.join('').includes('|'), sb);

  // ── H: A4.9 Important-1 — tab-indented code must not become a table ────
  const pyCode = 'def f():\n\t\tx = 1\n\t\ty = 2\n\t\treturn x';
  check('H1 double-tab-indented code untouched',
    convertTabTablesToMarkdown(pyCode) === pyCode, convertTabTablesToMarkdown(pyCode));
  const tabAligned = 'if a:\n\t\tpass\nelse:\n\t\tpass';
  check('H2 tab-indented branches untouched',
    convertTabTablesToMarkdown(tabAligned) === tabAligned);

  // ── I: A4.9 Minor-2 — mixed fence types must not leak rows ─────────────
  const mixedFence = '```python\nx = 1\n~~~\na\tb\tc\nd\te\tf\n```\n\nh1\tx\ty\nr1\tz\tw';
  const mf = convertTabTablesToMarkdown(mixedFence);
  check('I1 code rows untouched, real table after the fence converted',
    !mf.includes('| a | b | c |') && mf.includes('| h1 | x | y |'), mf);

  // ── J: A4.9 Minor-3 — literal backslash-pipe cannot forge a cell ───────
  const escapedPipe = 'a\tb\\|c\td\ne\tf\tg';
  const ep = convertTabTablesToMarkdown(escapedPipe);
  check('J1 backslash escaped before pipe',
    ep.split('\n')[0] === '| a | b\\\\\\|c | d |', ep.split('\n')[0]);

  // ── K: A4.9 Minor-4 — exact block cap (500 rows = one table) ───────────
  const row = (n) => `c1_${n}\tc2_${n}\tc3_${n}`;
  const exactly500 = Array.from({ length: 500 }, (_, n) => row(n)).join('\n');
  const at500 = convertTabTablesToMarkdown(exactly500).split('\n');
  check('K1 500-row block stays one table',
    at500.filter((l) => l.startsWith('| ---')).length === 1, at500.length);
  const over500 = Array.from({ length: 502 }, (_, n) => row(n)).join('\n');
  const at502 = convertTabTablesToMarkdown(over500).split('\n');
  check('K2 502 rows split into two blank-line-separated tables',
    at502.filter((l) => l.startsWith('| ---')).length === 2 && at502.includes(''), at502.length);
  const at501 = convertTabTablesToMarkdown(exactly500 + '\n' + row(500)).split('\n');
  check('K3 501 rows: 500-row table + 1 untouched row (no data loss)',
    at501.filter((l) => l.startsWith('| ---')).length === 1 &&
    at501[at501.length - 1] === row(500), at501.slice(-2));

  // ── L: marked round-trip — the converted text really renders <table> ──
  try {
    const markedMod = await import('marked');
    const marked = markedMod.marked || markedMod.default || markedMod;
    const html = marked.parse(convertTabTablesToMarkdown(pasted));
    check('L1 production paste renders a real <table>', html.includes('<table>') && html.includes('<th>命题</th>'), html.slice(0, 200));
    const htmlCode = marked.parse(convertTabTablesToMarkdown(pyCode));
    check('L2 indented code renders as code, not a table', !htmlCode.includes('<table>'), htmlCode.slice(0, 200));
  } catch (e) {
    check('L0 marked import', false, String(e));
  }

  console.log(failed === 0 ? '\nall checks pass' : `\n${failed} check(s) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
