/* 手机端语音模式切回文字（需求 5）接线不变量。
 *
 * 症状：手机端语音输入模式下，agent 回答过程中提出的 query 之后无法切回文字
 * 输入。根因：语音模式里唯一的出口是浮动「切回文字」按钮
 * （.voice-mode-toggle-btn--float），而它被
 * `:disabled="chatStore.isStreamingCurrentConversation"` 整段流式期间锁死；
 * 工具栏上的同款按钮在语音模式下整个 .input-main-area 是 display:none，
 * 且它还是 stop 按钮的 v-else 分支 —— 用户在语音模式下彻底没有出口。
 *
 * 切换输入方式是纯 UI 状态（isMobileVoiceMode / body.voice-mode-active），
 * 不应被流式状态锁死。
 *
 * Run: node tests/workflows/test_voice_toggle_wiring.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const SRC = path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatInput.vue');
const src = fs.readFileSync(SRC, 'utf8');

// 取出浮动按钮整块标记（自 <button 到匹配的 </button>）
const floatIdx = src.indexOf('voice-mode-toggle-btn--float');
check('T0 存在浮动切回文字按钮', floatIdx > 0);
let blockStart = src.lastIndexOf('<button', floatIdx);
const blockEnd = src.indexOf('</button>', floatIdx);
const block = blockStart >= 0 && blockEnd > blockStart ? src.slice(blockStart, blockEnd) : '';

check('T1 浮动按钮不被流式状态禁用（可随时切回文字）',
  !/isStreamingCurrentConversation/.test(block), { block: block.slice(0, 300) });
check('T2 浮动按钮不存在 :disabled 绑定',
  !/:disabled=/.test(block), { block: block.slice(0, 300) });
check('T3 点击仍走 onVoiceToggleClick（切换语义未被拆除）',
  /onVoiceToggleClick/.test(block));
check('T4 切换状态仍是 isMobileVoiceMode 单一事实源',
  /isMobileVoiceMode/.test(src) && /voice-mode-active/.test(src));

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
