// 判据 #29 pin — htmlToMarkdown 列表边界屏障不变量（源码 pin，弱层；
// 行为主证据 = e2e/note_word_copy_fix.spec.ts C29a/b/c）。
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..');
const useMd = fs.readFileSync(path.join(root, 'src', 'composables', 'useMarkdown.ts'), 'utf8');
const editor = fs.readFileSync(path.join(root, 'src', 'components', 'WysiwygEditor.vue'), 'utf8');

let failed = 0;
function check(ok, msg) {
  if (ok) console.log('PASS', msg);
  else { console.error('FAIL', msg); failed++; }
}

// 1) 相邻列表块之间必须插入 `<!--list-break-->` 屏障（marked 拆列表；DOMPurify 渲染期剥离）
check(useMd.includes("'<!--list-break-->'") || useMd.includes('<!--list-break-->'),
  'useMarkdown.processChildren 带列表边界屏障标记');
check(useMd.includes('lastWasList'),
  'processChildren 维护列表边界状态（空白输出不重置）');

// 2) 破损逃生口必须移除：`<ol style>` 包 markdown 内文 → 无 <li> 裸文本
check(!useMd.includes("'<ol style=\"counter-reset:'"),
  '旧 counterReset 裸 HTML 序列化逃生口已移除');

// 3) li 内输入 "N." 的 hoist 语义（D-101：与 restartOlNumbering 嵌套分支一致）
check(editor.includes('outermostList'),
  'tryAutoNumberedList 提供最外层列表 hoist');
check(editor.includes("text.match(/^(\\d+)\\.\\s?$/)"),
  '自动列表标记匹配保持不变');

// 4) LI 宿主形态（空项重敲 "1." = 从这里重新编号）
check(/block\.tagName === 'LI'/.test(editor),
  'tryAutoNumberedList 处理 LI 宿主（列表项内重起编号）');

// ---- 盲区清偿波 pin（判据 #39）----
check(useMd.includes("case 'li'"), 'case li 独立序列化路径存在');
check(/case 'li'[\s\S]{0,1600}list-break/.test(useMd), 'case li 同层相邻嵌套列表同屏障（#39）');

if (failed) {
  console.log('RESULT: ' + failed + ' failed');
  process.exit(1);
}
console.log('RESULT: ALL CHECKS PASS');
