/* endnote 全链穿线源检（挂账清理波 T4 / D-18）：尊重 UI 开关（wt-endnote-enabled）
 * ——bulkExportNotes 查询参数 · export-tasks 任务载荷（single+bulk）。
 * Run: node --experimental-strip-types tests/workflows/test_endnote_threading.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

const apiSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'api', 'notes.ts'), 'utf-8');
const storeSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'notes.ts'), 'utf-8');

check('E1 bulkExportNotes 发 endnote 查询参数',
  /bulkExportNotes[\s\S]{0,400}?endnoteExportQuery\(\)/.test(apiSrc));
check('E2 任务载荷 single 带 endnote_enabled',
  /task_type: 'single'[\s\S]{0,120}?endnote_enabled: endnoteExportQuery\(\)\.endnote_enabled/.test(storeSrc));
check('E3 任务载荷 bulk 带 endnote_enabled',
  /task_type: 'bulk'[\s\S]{0,120}?endnote_enabled: endnoteExportQuery\(\)\.endnote_enabled/.test(storeSrc));
const chatAreaSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'ChatArea.vue'), 'utf-8');
const convHits = (chatAreaSrc.match(/endnote_enabled: endnoteExportQuery\(\)\.endnote_enabled/g) || []).length;
check('E4 对话导出（single+bulk）带 endnote_enabled', convHits === 2);

if (failed) {
  console.error(`\n${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('\nall checks pass');
