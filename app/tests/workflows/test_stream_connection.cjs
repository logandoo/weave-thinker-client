/* streamConnection 不变量测试（Node 直跑，type stripping）。
 * part A: abortPredecessor 行为（纯函数）
 * part B: chat.ts 接线断言——resumeActiveStream 必须在安装新 controller
 *         之前调用 abortPredecessor（事故 conv 3a216a51 双连接双写修复）
 * Run: node tests/workflows/test_stream_connection.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

// ── part A ──────────────────────────────────────────────────────────────────
async function main() {
  const mod = await import('../../src/stores/streamConnection.ts');
  const { abortPredecessor } = mod;

  let aborted = 0;
  const fake = { abort() { aborted += 1; } };
  const holder = { abortController: fake };
  check('A1 aborts live predecessor', abortPredecessor(holder) === true && aborted === 1);

  const throwing = { abort() { throw new Error('boom'); } };
  check('A2 suppresses abort() error', abortPredecessor({ abortController: throwing }) === true);

  check('A3 no predecessor → false, no throw', abortPredecessor({ abortController: null }) === false);
  check('A4 undefined predecessor → false', abortPredecessor({}) === false);

  // ── part B: wiring ────────────────────────────────────────────────────────
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf-8');
  const fnStart = src.indexOf('async function resumeActiveStream');
  check('B1 resumeActiveStream exists', fnStart > 0);
  const fnBody = src.slice(fnStart, src.indexOf('\n  }', fnStart + 100) + 4);
  const wirePos = fnBody.indexOf('abortPredecessor(s)');
  const newPos = fnBody.indexOf('new AbortController()');
  check('B2 resumeActiveStream calls abortPredecessor(s)', wirePos > 0);
  check('B3 abortPredecessor before new AbortController', wirePos > 0 && newPos > 0 && wirePos < newPos);
  check('B4 import wired', /import\s*\{[^}]*abortPredecessor[^}]*\}\s*from\s*'(@|\.)(\/stores)?\/streamConnection'/.test(src));
  // B5: wireStreamCallbacks 连接令牌守卫（迟到事件入口丢弃——积压回放双写的运行时后盾）
  const wireStart = src.indexOf('function wireStreamCallbacks');
  const wireBody = src.slice(wireStart, src.indexOf('\n  }\n', wireStart) + 4);
  check('B5 wireStreamCallbacks token guard wired', /const connection = s\.abortController/.test(wireBody) && wireBody.includes('s.abortController !== connection'));

  // ── part C: isNoneReplayTransient 纯函数（P1 2026-09-05 手机切后台断直播） ──
  // resume 收 'none' 回放双义：run 真不存在（终态）vs SETUP 期无 buffer（暂态，
  // 终态化会把 streaming 掐死、只剩最后一条 query 而生成仍在后台继续）。
  const { isNoneReplayTransient } = mod;
  check('C1 setup_in_progress → 暂态', isNoneReplayTransient({ has_buffer: false, status: 'incomplete', is_running: true, setup_in_progress: true }) === true);
  check('C2 live buffer → 暂态', isNoneReplayTransient({ has_buffer: true, status: 'incomplete', is_running: true, setup_in_progress: false }) === true);
  check('C3 真无 run → 终态', isNoneReplayTransient({ has_buffer: false, status: 'none', is_running: false, setup_in_progress: false }) === false);
  check('C4 已完成的 buffer → 终态', isNoneReplayTransient({ has_buffer: true, status: 'complete', is_running: false, setup_in_progress: false }) === false);
  check('C5 查询失败 → 终态（不堵死恢复）', isNoneReplayTransient({ error: true }) === false);
  check('C6 null/undefined → 终态', isNoneReplayTransient(null) === false && isNoneReplayTransient(undefined) === false);

  // ── part D: P1 接线断言 ───────────────────────────────────────────────────
  // D1: 'none' 分支必须 consult isNoneReplayTransient 且暂态时 return false
  //     （而不是无条件 endStreaming + return true）。
  check('D1 resumeActiveStream none-暂态 return（不终态化）', /replayStatus === 'none'/.test(fnBody) && fnBody.includes('isNoneReplayTransient') && /if \(transient[\s\S]{0,220}?return 'failed'/.test(fnBody));
  // D2: syncAfterAbort 带 allowReattach 参数且默认 false（STOP 路径绝不重挂）。
  const syncStart = src.indexOf('async function syncAfterAbort');
  const syncHead = src.slice(syncStart, src.indexOf(')', src.indexOf('partialReasoningContent', syncStart)) + 1);
  check('D2 syncAfterAbort allowReattach 默认 false', /allowReattach\s*=\s*false/.test(syncHead));
  // D3: Phase-2 轮询在 live buffer 出现时重挂直播。
  const syncBody = src.slice(syncStart, src.indexOf('\n  }\n', syncStart + 200) + 4);
  check('D3 Phase-2 live buffer 重挂 resumeActiveStream', /allowReattach[\s\S]{0,400}?resumeActiveStream\(conversationId\)/.test(syncBody));
  // D4: tryReconnectOrSync 传 allowReattach=true。
  const recStart = src.indexOf('async function tryReconnectOrSync');
  const recBody = src.slice(recStart, src.indexOf('\n  }\n', recStart) + 4);
  check('D4 tryReconnectOrSync 传 true', /syncAfterAbort\([^)]*true\s*\)/.test(recBody));
  // D5: selectConversation 有 setup_in_progress 分支。
  const selStart = src.indexOf('async function selectConversation');
  const selBody = src.slice(selStart, src.indexOf('\n  }\n', selStart) + 4);
  check('D5 selectConversation setup_in_progress 分支', /else if \(status\.setup_in_progress\)/.test(selBody));
  // D6: visibility 处理器对失活连接不再无条件保留。
  const vis = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chatVisibility.ts'), 'utf-8');
  check('D6 visibility 失活阈值接线', /VISIBILITY_STALE_MS/.test(vis) && /silentMs\s*(?:<=|>|<|>=)\s*VISIBILITY_STALE_MS/.test(vis));
  // D7 (A4.9 R1 I1): 'none' 终态收尾前必须有连接令牌检查（getStreamStatus
  // RTT 期间被接管 → return false，不得 endStreaming 接管方的状态）。
  check('D7 none-终态前令牌守卫', /if \(s\.abortController !== abortController\) \{\s*return 'failed'\s*\}/.test(fnBody));

  if (failed) {
    console.error(`\n${failed} check(s) FAILED`);
    process.exit(1);
  }
  console.log('\nall checks pass');
}

main().catch(e => { console.error(e); process.exit(1); });
