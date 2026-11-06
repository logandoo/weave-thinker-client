// 判据 #28/#31 pin — 复制净化通道接线与净化不变量（源码 pin，弱层；
// 行为主证据 = e2e/note_word_copy_fix.spec.ts C28/C31）。
const fs = require('fs');
const path = require('path');

const root = path.join(__dirname, '..', '..');
const cleanCopy = fs.readFileSync(path.join(root, 'src', 'composables', 'cleanCopy.ts'), 'utf8');
const mainTs = fs.readFileSync(path.join(root, 'src', 'main.ts'), 'utf8');
const editor = fs.readFileSync(path.join(root, 'src', 'components', 'WysiwygEditor.vue'), 'utf8');
const useMd = fs.readFileSync(path.join(root, 'src', 'composables', 'useMarkdown.ts'), 'utf8');

let failed = 0;
function check(ok, msg) {
  if (ok) console.log('PASS', msg);
  else { console.error('FAIL', msg); failed++; }
}

// 1) 模块导出 + 全局注册
check(cleanCopy.includes('export function buildCleanCopyHtml'), 'buildCleanCopyHtml 导出');
check(cleanCopy.includes('export function installGlobalCleanCopy'), 'installGlobalCleanCopy 导出');
check(mainTs.includes('installGlobalCleanCopy()'), 'main.ts 注册复制净化');

// 2) 字体只在段级（D-102）：统一中文栈 + 代码等宽栈；行级 run 零字体
check(cleanCopy.includes("font-family:'PingFang SC','Microsoft YaHei',sans-serif"), '段级中文字体栈');
check(cleanCopy.includes("font-family:Consolas,'Courier New',monospace"), '代码等宽字体栈');

// 3) 横线源（判据 #31）：hr → 可删除文本线；border 仅 td/th；无 border-bottom
check(cleanCopy.includes("'──────'"), 'hr 降级为可删除文本分隔线');
check(!cleanCopy.includes('border-bottom:'), '净化产物无 border-bottom 样式声明');
check(cleanCopy.includes('border:1px solid #999'), '表格边框显式 td/th');

// 4) 界面 chrome 不进剪贴板
check(cleanCopy.includes('.code-block-header') && cleanCopy.includes('.mermaid-controls'), '工具条/控件剥离');

// 5) sup/sub 范围级切换（判据 #30）
check(editor.includes('function toggleScriptTag'), 'toggleScriptTag 共享切换');
check(editor.includes('function splitElementAt') && editor.includes('function unwrapRangeInTag'),
  '范围级拆分解包（部分还原不连带）');
check(editor.includes("e.inputType === 'insertText'"), 'sup/sub 边界输入护栏（手动插入）');

// ---- 盲区清偿波 P1-P2 pin（判据 #33/#34/#36/#38/#40/#43）----
check(cleanCopy.includes("addEventListener('cut'"), 'cut 通道接管（#33）');
check(cleanCopy.includes('StartFragment'), 'CF_HTML 包装（#43）');
check(cleanCopy.includes('export function buildCleanCopyText'), '纯文本派生（#36）');
check(cleanCopy.includes('text-align'), '对齐保留（#37）');
check(editor.includes("e.inputType === 'insertReplacementText'"), 'insertReplacementText 护栏（#34）');
check(editor.includes("e.inputType === 'insertHTML'"), 'insertHTML 护栏（#34）');
check(editor.includes('function cleanupEmptyInline'), '空行内残壳清理（#38）');
check(editor.includes('function pruneEmptyLis'), '深层空 li 回收（#39）');
check(useMd.includes('export function splitNumberingResets'), '粘贴编号重置拆分（#40）');

check(cleanCopy.includes('wrapFragmentMarkers') && !cleanCopy.includes('Version:0.9'), '不自发 CF_HTML 头（D-105 评审 A C1/B C2）');
check(editor.includes("e!.data || e!.dataTransfer"), 'insertHTML 负载双取 e.data/dataTransfer（评审 B I1）');

if (failed) {
  console.log('RESULT: ' + failed + ' failed');
  process.exit(1);
}
console.log('RESULT: ALL CHECKS PASS');
