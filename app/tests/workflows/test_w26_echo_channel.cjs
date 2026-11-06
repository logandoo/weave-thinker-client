/* W26 echo 通道（HANDOFF_upstream_w26 §B1/B2/B3）跨栈接线不变量。
 *
 * 症状：插话只发一次却显示两条 query 气泡；断连期 commit 的插话永不确认。
 * 服务端根因：
 *   B1 registry broadcast 溢出无差别丢队首（契约事件与 delta 同权重）
 *   B2 resume replay 载荷不含插话 echo（buffer 无归一化分支 / to_replay 无字段）
 *   B3 echo 按 content 配对 + 无连接令牌守卫
 *
 * 本测试钉住服务端与前端的接线点（行为级证据由
 * backend/tests/test_interjection_echo_replay.py 与 E2E 承担）：
 *   E1 B1 契约事件白名单 + 溢出先淘汰最旧非契约项
 *   E2 B2 Buffer 记录 interjections_committed 并在 to_replay/to_snapshot/
 *      快照持久化/跨 worker 恢复暴露
 *   E3 B2 resume 实时增量转发 interjection_committed
 *   E4 B3 interject POST 接受 client_message_id、echo 回带、drain 兼容 str/dict
 *   E5 前端 replay 应用 echo；live handler 有连接令牌守卫；按 id 配对并保留
 *      内容兜底；POST 发送 client_message_id
 *
 * Run: node tests/workflows/test_w26_echo_channel.cjs */

const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const ROOT = path.resolve(__dirname, '..', '..', '..');
const read = (...p) => fs.readFileSync(path.join(ROOT, ...p), 'utf8');

// vendor 布局适配（W27 同步；同 test_server_drain 先例）：上游 repo 用 backend/，
// 本仓 [[map]] 映射为 server/——仅路径段替换，断言面零改动。
const registrySrc = read('server', 'app', 'services', 'active_agent_registry.py');
const bufferSrc = read('server', 'app', 'services', 'stream_buffer.py');
// C-02/C-02c: the SSE transport (and the interjection echo it owns) moved out
// of chat.py into chat_stream_transport.py — scan both, else the wiring check
// silently rots against a file that no longer holds the code.
const chatPySrc = read('server', 'app', 'api', 'chat.py')
  + read('server', 'app', 'api', 'chat_stream_transport.py');
const agentLoopSrc = read('server', 'app', 'services', 'agent_loop.py');
const dispatchSrc = read('app', 'src', 'api', 'streamDispatch.ts');
const chatApiSrc = read('app', 'src', 'api', 'chat.ts');
const chatStoreSrc = read('app', 'src', 'stores', 'chat.ts');

// ── E1 B1 契约事件不丢 ───────────────────────────────────────────────────
const evictStart = registrySrc.indexOf('def _enqueue_with_contract_priority');
const evictBody = evictStart >= 0 ? registrySrc.slice(evictStart, evictStart + 2600) : '';
check('E1a 契约事件白名单含真实广播形态（interjection_committed/_completed/_failed/done/error/permission_request）', (() => {
  const m = registrySrc.match(/_CONTRACT_EVENT_KEYS\s*=\s*\(([\s\S]*?)\)/);
  return !!m && ['interjection_committed', '_completed', '_failed', 'done', 'error', 'permission_request'].every(k => m[1].includes(`"${k}"`));
})());
check('E1b 溢出走统一入队助手（含契约判定，非无差别单发淘汰）', (() => {
  const start = registrySrc.indexOf('async def broadcast(');
  const end = registrySrc.indexOf('async def broadcast_done(');
  const body = start >= 0 && end > start ? registrySrc.slice(start, end) : '';
  return !!body && /_enqueue_with_contract_priority\(q, event\)/.test(body) && !/q\.get_nowait\(\)/.test(body);
})());
check('E1c 淘汰目标=最旧非契约项（保序回填 + 追加新事件）', /_is_contract_event\(item\)/.test(evictBody) && /pending\.pop\(drop_idx\)/.test(evictBody) && /for item in pending:[\s\S]{0,80}?q\.put_nowait\(item\)/.test(evictBody));
check('E1d 终态哨兵（broadcast_done）也走契约优先入队', /def broadcast_done[\s\S]{0,400}?_enqueue_with_contract_priority\(q, sentinel\)/.test(registrySrc));

// ── E2 B2 Buffer 记录 + replay/snapshot 暴露 ─────────────────────────────
check('E2a ConversationBuffer 声明 interjections_committed 字段', /interjections_committed:\s*List\[dict\]\s*=\s*field\(default_factory=list\)/.test(bufferSrc));
check('E2b append 有 interjection_committed 归一化分支', /elif "interjection_committed" in event:/.test(bufferSrc));
check('E2c to_replay 暴露 interjections_committed', /def to_replay[\s\S]{0,2200}?"interjections_committed":\s*list\(self\.interjections_committed\)/.test(bufferSrc));
check('E2d to_snapshot 暴露 interjections_committed', /def to_snapshot[\s\S]{0,2200}?"interjections_committed":\s*list\(self\.interjections_committed\)/.test(bufferSrc));
check('E2e 跨 worker 恢复赋值 interjections_committed', /buf\.interjections_committed\s*=\s*\[dict\(x\) for x in snap\.get\("interjections_committed", \[\]\)\]/.test(bufferSrc));
check('E2f 快照循环持久化 interjections_committed', /"interjections_committed":\s*\[dict\(x\) for x in buf\.interjections_committed\]/.test(bufferSrc));

// ── E3 B2 resume 实时增量转发 ────────────────────────────────────────────
check('E3a _forward_delta 转发 interjection_committed（message 事件）', /delta\["type"\] == "interjection_committed"[\s\S]{0,600}?"interjection_committed":\s*delta\["data"\]/.test(chatPySrc));

// ── E4 B3 client_message_id 全链 ─────────────────────────────────────────
check('E4a 端点 helper 规范化 content/client_message_id', /def _normalize_interject_request\(/.test(chatPySrc));
check('E4b 端点按 structured payload 入队（含 cmid）', /state\.interjection_queue\.put_nowait\(\{\s*"content": content,\s*"client_message_id": client_message_id,?\s*\}\)/.test(chatPySrc));
check('E4c producer echo 回带 client_message_id', /"client_message_id":\s*_ij_client_message_id/.test(chatPySrc));
const drainStart = agentLoopSrc.indexOf('def _drain_interjections');
const drainBody = drainStart >= 0 ? agentLoopSrc.slice(drainStart, drainStart + 4000) : '';
check('E4d drain 兼容 dict 队列项（agent_worker 仍投 str）', /isinstance\(item, dict\)/.test(drainBody) && /_interjection": True/.test(drainBody));
check('E4e 空文本 dict 项被跳过（不产生空插话）', /if not text\.strip\(\):\s*\n\s*continue/.test(drainBody));

// ── E5 前端 replay/配对/守卫/请求体 ──────────────────────────────────────
const pairingSrc = read('app', 'src', 'stores', 'interjectionPairing.ts');
check('E5a ReplayPayload 含 interjections_committed', /interjections_committed\?:\s*InterjectionEcho\[\]/.test(dispatchSrc));
check('E5b Echo 载荷类型含 client_message_id', /interface InterjectionEcho[\s\S]{0,400}?client_message_id\?:/.test(dispatchSrc));
check('E5c onReplay 先应用 replay 的 echo（先于 early return）', /onReplay: \(replay\) => \{\s*\n\s*touch\(\)[\s\S]{0,400}?_applyInterjectionEchoes\(conversationId, replay\.interjections_committed/.test(chatStoreSrc));
check('E5d live echo handler 有连接令牌守卫', /onInterjectionCommitted: \(payload\) => \{[\s\S]{0,400}?if \(s\.abortController !== connection\) return/.test(chatStoreSrc));
check('E5e 配对纯函数：id 优先 + 无 id 内容兜底', /p\.clientMessageId === payload\.client_message_id/.test(pairingSrc) && /x\.p\.content === payload\.content/.test(pairingSrc));
check('E5h 有 id 时只按 id（内容分支在 else 内）', /if \(payload\.client_message_id\) \{[\s\S]{0,300}?return pending\.findIndex[\s\S]{0,200}?\}\s*\n\s*if \(!payload\.content\) return -1/.test(pairingSrc));
check('E5i replay 无 id echo 不确认带 cmid 新条目（有界回落仅 live）', /!opts\?\.fromReplay \|\| !x\.p\.clientMessageId/.test(pairingSrc) && /if \(!pick && !opts\?\.fromReplay\)/.test(pairingSrc));
check('E5j chat.ts 使用配对纯函数', /import \{ pairInterjectionEcho \} from '@\/stores\/interjectionPairing'/.test(chatStoreSrc) && /pairInterjectionEcho\(pend, payload, opts\)/.test(chatStoreSrc));
check('E5k 配对行为级单测接入 test:unit', /test_interjection_pairing\.mjs/.test(fs.readFileSync(path.join(ROOT, 'app', 'package.json'), 'utf8')));
check('E5f 插话 POST 携带 client_message_id', /body\.client_message_id = clientMessageId/.test(chatApiSrc));
check('E5g pending 条目记录 clientMessageId 并随请求发送', /clientMessageId \}\)/.test(chatStoreSrc) && /interjectStream\(convId, text, clientMessageId\)/.test(chatStoreSrc));

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
