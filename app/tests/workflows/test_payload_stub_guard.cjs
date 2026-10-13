// 接线守卫：payload 取回失败必须可感知（「截断必须可感知」红线），
// 2026-10-06 W3 随上游重写（服务端 404 硬化 + per-field inflight + MessageBubble
// 可重试状态机）更新为同契约的新形态断言。旧形态（ensureMessageFull 内联
// isExternalizedStub 守卫）的前提=弱路由，已由上游路由硬化（归属寻址+完整性 404）
// 消除；存储层测试见 server/tests/test_message_payload_route_scoping.py。
const fs = require('fs');
const path = require('path');

const CHAT = path.resolve(__dirname, '../../src/stores/chat.ts');
const BUBBLE = path.resolve(__dirname, '../../src/components/MessageBubble.vue');
const chat = fs.readFileSync(CHAT, 'utf8');
const bubble = fs.readFileSync(BUBBLE, 'utf8');

let failed = 0;
function check(name, ok) {
  console.log((ok ? 'PASS ' : 'FAIL ') + name);
  if (!ok) failed++;
}

// 1) store：按字段取回，失败以 false 上抛（调用方可渲染失败态）
const fetchFnStart = chat.indexOf('async function _fetchFieldFull');
const fetchFnEnd = chat.indexOf('async function', fetchFnStart + 10);
const fetchBody = chat.slice(fetchFnStart, fetchFnEnd === -1 ? undefined : fetchFnEnd);
check('G1 _fetchFieldFull 存在', fetchFnStart > 0);
check('G2 取回仍走 getMessagePayload（展开面未绕开）', fetchBody.includes('chatApi.getMessagePayload'));
check('G3 失败分支返回 false（可感知，非静默）', /catch\s*\{[\s\S]{0,160}return\s+false/.test(fetchBody));
check('G3b 200-桩双保险：返回内容仍是桩 → return false（防静默预览+重取环）',
  /isExternalizedStub\(\s*payload\.content\s*\)[\s\S]{0,60}return\s+false/.test(fetchBody));
check('G4 写回按 id 补丁（避免入口快照整体替换竞态）',
  /findIndex\(\(m\) => m\.id === messageId\)/.test(fetchBody));

// 2) MessageBubble：失败必须落到可见可重试 UI（红线承接）
check('G5 组件存在 retryFetchFull（可重试入口）', bubble.includes('retryFetchFull'));
check('G6 存在可见重试按钮 .fetch-retry 且绑定 retryFetchFull（绑定级，非仅函数定义）',
  /class="fetch-retry"[^>]*@click\.stop="retryFetchFull\(/.test(bubble));
check('G7 per-field 失败态 state 机且错误角标读取状态映射（非常量）',
  /payloadFetchByField/.test(bubble) && /Object\.values\(payloadFetchByField\.value\)\.some/.test(bubble)
  && /'error'/.test(bubble) && /'loading'/.test(bubble));

// 3) 守卫前提仍成立：路由层 404 硬化在位（server 侧测试同步断言，见头部注释）
const routeTest = path.resolve(__dirname, '../../../server/tests/test_message_payload_route_scoping.py');
check('G8 server 路由守卫测试在盘（伪造/孤儿/完整性 404）', fs.existsSync(routeTest));

console.log(failed ? `RESULT: ${failed} failed` : 'RESULT: all pass');
process.exit(failed ? 1 : 0);
