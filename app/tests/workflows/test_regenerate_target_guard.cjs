/* regenerate 目标守卫 + 404 文案透传 接线断言（conv 92398a80 生产事故 2026-10-17）。
 *
 * 事故：regenerateLastAssistantMessage 取本地最后一条 assistant 消息 id 直发，
 * 不过滤合成 id（temp-/bg-/resume- 等）也不先刷新——stale/合成目标让服务端
 * _trim_messages_for_regeneration 404「待重新生成的消息不存在」，用户只见
 * 裸「HTTP error: 404」。
 *
 * 契约：
 *  G1 regenerate 目标过滤：只接受服务器持久化 id（_isServerMessageId 且非
 *     isSyntheticMessageId）；
 *  G2 本地无有效目标 → refreshConversation 后重解析；仍无 → 中文提示并 return，
 *     不发注定 404 的请求；
 *  G3 api/chat.ts 非 2xx 透传 body.detail 进 onError（不再只有 HTTP error: NNN）。
 *
 * Run: node tests/workflows/test_regenerate_target_guard.cjs
 */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

const storeSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf-8');
const apiSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'api', 'chat.ts'), 'utf-8');

// ── G1/G2：regenerateLastAssistantMessage 守卫 ──
const fnStart = storeSrc.indexOf('async function regenerateLastAssistantMessage');
check('G0 regenerateLastAssistantMessage exists', fnStart > 0);
// 函数体截取到下一个顶层函数声明
const nextFn = storeSrc.indexOf('\n  async function', fnStart + 10);
const body = storeSrc.slice(fnStart, nextFn > fnStart ? nextFn : fnStart + 6000);

check('G1a target filtered by _isServerMessageId',
  /_isServerMessageId\(item\.message\.id\)/.test(body));
check('G1b target filtered by isSyntheticMessageId',
  /!isSyntheticMessageId\(item\.message\.id\)/.test(body));
check('G2a refresh fallback before giving up', /await refreshConversation\(convId\)/.test(body));
check('G2b clear Chinese error instead of doomed request',
  body.includes('没有可重新生成的回答') && /return/.test(body));

// ── G3：404 detail 透传 ──
const errIdx = apiSrc.indexOf('if (!response.ok)');
check('G3a streamChat has !response.ok branch', errIdx > 0);
const errBlock = apiSrc.slice(errIdx, errIdx + 1200);
check('G3b server detail surfaced in onError',
  /body\?\.detail|\.detail/.test(errBlock) && /onError\(/.test(errBlock));
check('G3c HTTP status kept as fallback',
  errBlock.includes('HTTP error:'));

// ── 评审 fix-round（2026-10-18）────────────────────────────────
// B-I1：姊妹入口 regenerateWithForceResults 同守卫（合成/失效 id 不直发）
const fn2Start = storeSrc.indexOf('async function regenerateWithForceResults');
check('H0 regenerateWithForceResults exists', fn2Start > 0);
const nextFn2 = storeSrc.indexOf('\n  async function', fn2Start + 10);
const body2 = storeSrc.slice(fn2Start, nextFn2 > fn2Start ? nextFn2 : fn2Start + 6000);
check('H1 force-results target filtered by server-id predicates',
  /_isServerMessageId\(/.test(body2) && /isSyntheticMessageId\(/.test(body2));
check('H2 force-results missing target → 中文提示 return（不发 doomed 请求）',
  body2.includes('没有可重新生成的回答') || body2.includes('已失效'));

// B-M1：G2b 加固——提示文案必须在 streamChat 调用之前且两者之间有 return
const errPos = body.indexOf('没有可重新生成的回答');
const callPos = body.indexOf('chatApi.streamChat');
const between = errPos > 0 && callPos > errPos ? body.slice(errPos, callPos) : '';
check('H3 guard fires before any streamChat call', errPos > 0 && callPos > errPos);
check('H4 return between guard and streamChat', /\breturn\b/.test(between));

if (failed) {
  console.error(`\n${failed} check(s) FAILED`);
  process.exit(1);
}
console.log('\nALL CHECKS PASS');
