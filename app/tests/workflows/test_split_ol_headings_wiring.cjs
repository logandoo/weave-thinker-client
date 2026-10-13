/* splitOlContainingHeadings 锚点错位回归钉（W12 Dx-2）。
 *
 * 事故：60 生产思考区（reasoning-text）整段裸文本——`N. ### …` 大纲体触发
 * splitOlContainingHeadings，旧实现 `ol.parentNode?.insertBefore(newH2, li.nextSibling)`
 * 的参照节点（li.nextSibling，marked 输出里恒为 "\n" 文本节点）是 ol 的子节点，
 * 不是调用宿主 ol.parentNode 的子节点 → insertBefore 必抛 NotFoundError →
 * 被 renderMarkdownToHtml 的静默 catch 吞掉 → 全文恒等回退（格式全丢）。
 *
 * 行为级断言由 Playwright e2e/reasoning_render.spec.ts 承担（需真实 DOM 解析）。
 * 本钉守源码面：锚点形态 + 余项保留 + catch 可观测，防回潮。
 *
 * Run: node tests/workflows/test_split_ol_headings_wiring.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const src = fs.readFileSync(
  path.resolve(__dirname, '../../src/composables/useMarkdown.ts'), 'utf8');

// 截取 splitOlContainingHeadings 函数体（剥行注释，防病灶文档注释自匹配）
const stripComments = (s) => s.replace(/^\s*\/\/.*$/gm, '');
const fnStart = src.indexOf('function splitOlContainingHeadings');
check('splitOlContainingHeadings 存在', fnStart > -1);
const fnEnd = src.indexOf('\nfunction ', fnStart + 10);
const body = stripComments(src.slice(fnStart, fnEnd > fnStart ? fnEnd : src.length));

// 1. 旧病灶：以 ol.parentNode 为宿主却用 ol 的子节点做参照（必抛 NotFoundError）
check('无 ol.parentNode.insertBefore(*, li.nextSibling) 错位锚点',
  !/ol\.parentNode\?*\.insertBefore\([^)]*li\.nextSibling/.test(body));

// 2. 新锚点：宿主 = li 实时父 ol 的父节点，参照 = 该 ol 自身
check('锚点经 curOl/host 重新归位', /host\.insertBefore\(heading, curOl\.nextSibling\)/.test(body));

// 3. 逐项重新锚定（多标题列表：拆分会搬动后续 li，快照锚点会陈旧）
check('每轮取 li.parentElement 为 curOl', /const curOl = li\.parentElement/.test(body));

// 4. 余项保留：只拔标题，li 含其他文字时不整删
check('标题 li 余项保留（仅空 li 才移除）', /!li\.textContent\?\.trim\(\)/.test(body));

// 5. renderMarkdownToHtml 的裸回退不再静默（错误消息可观测，不带 content）
const renderStart = src.indexOf('export function renderMarkdownToHtml');
const renderEnd = src.indexOf('\nexport function ', renderStart + 10);
const renderBody = src.slice(renderStart, renderEnd > renderStart ? renderEnd : src.length);
check('renderMarkdownToHtml catch 可观测（console.warn）',
  /catch \(err\)[\s\S]*?console\.warn\(/.test(renderBody));
check('renderMarkdownToHtml 无静默裸 catch', !/\} catch \{\s*return content\s*\}/.test(renderBody));

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
