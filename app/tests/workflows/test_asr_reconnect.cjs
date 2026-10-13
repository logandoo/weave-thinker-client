/* useAsrStreaming 断线重连接线不变量（Node 直跑）。
 *
 * 背景（wave-4 判据#13）：听写面「很容易断开」的第二重根因——服务端报
 * `error` 事件后客户端 `handleSocketMessage` 直接 `cleanupState()+clearSocket()`，
 * `onclose` 被置空 → `handleSocketClose` 不再触发 → `scheduleReconnect` 整体被
 * 绕过，一次瞬断即整段听写硬停。服务端已改以 1011 异常收尾
 * （asr_service._notify_upstream_drop），客户端须把录音中的 error 交给关闭事件。
 *
 * 本文件是**接线不变量**测试（沿用 test_promote_wiring / test_grilling_wiring
 * 同款约定：composable 走 `@/` 别名无法被 Node 直接 import，故以源码接线断言
 * 覆盖）。服务端腿的行为级证据见 server/tests/test_asr_dictation_keepalive.py。
 *
 * Run: node tests/workflows/test_asr_reconnect.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${cond ? '' : ' — ' + (detail || '')}`);
  if (!cond) failed += 1;
}

const SRC = fs.readFileSync(
  path.join(__dirname, '../../src/composables/useAsrStreaming.ts'),
  'utf8',
);

// ── 1. error 分支：录音中不得拆会话（否则 onclose 被置空、重连被绕过）──
{
  const errIdx = SRC.indexOf("payload.event === 'error'");
  check('1.1 error 分支存在', errIdx > 0);
  const branch = SRC.slice(errIdx, errIdx + 900);
  const guardIdx = branch.indexOf('if (isRecording.value && !payload.fatal)');
  const cleanupIdx = branch.indexOf('cleanupState()');
  check('1.2 录音态（且非致命）守卫在 cleanupState 之前', guardIdx > 0 && cleanupIdx > guardIdx,
    `guard=${guardIdx} cleanup=${cleanupIdx}`);
  const retIdx = branch.indexOf('return', guardIdx);
  check('1.3 录音态走 return（不落 cleanupState/clearSocket）',
    retIdx > guardIdx && retIdx < cleanupIdx, `return=${retIdx} cleanup=${cleanupIdx}`);
  check('1.4 停止/未录音态仍拆会话（非录音 error 不得挂死）', cleanupIdx > 0);
  // 去掉 // 注释再断言（守卫块注释里以 clearSocket() 作反例说明，会误命中）
  const strip = s => s.replace(/\/\/[^\n]*/g, '');
  const guardBlock = strip(branch.slice(guardIdx, retIdx));
  check('1.5 录音态不 clearSocket（保 onclose 接棒）',
    guardBlock.indexOf('clearSocket()') === -1, guardBlock.slice(0, 160));
}

// ── 2. 重连成功后清错误态，UI 不停留在报错 ──
{
  const rcIdx = SRC.indexOf('async function reconnect()');
  check('2.1 reconnect() 存在', rcIdx > 0);
  const fn = SRC.slice(rcIdx, SRC.indexOf('function handleSocketClose', rcIdx));
  const clearErr = fn.indexOf('error.value = null');
  const flushing = fn.indexOf('flushPendingChunks()');
  check('2.2 重连成功清 error.value', clearErr > 0);
  check('2.3 清错发生在恢复音频之前', clearErr > 0 && flushing > clearErr,
    `clear=${clearErr} flush=${flushing}`);
}

// ── 3. 关闭事件仍驱动重连（异常关闭 + 录音中）──
{
  const csIdx = SRC.indexOf('function handleSocketClose');
  check('3.1 handleSocketClose 存在', csIdx > 0);
  const fn = SRC.slice(csIdx, SRC.indexOf('async function connectAudioProcessing', csIdx));
  check('3.2 停止码集合恰为 1000/1005/1008（不得把 1011 判为停止）',
    fn.includes('closeEvent.code === 1000') && fn.includes('closeEvent.code === 1005')
    && fn.includes('closeEvent.code === 1008') && !fn.includes('closeEvent.code === 1011'),
    '1011=上游瞬断，若入停止码集则每次瞬断硬停');
  // onclose 绑定是重连的唯一入口：createSocket 里若漏绑/被挪走，重连整体失效
  const coIdx = SRC.indexOf('function createSocket');
  const coFn = SRC.slice(coIdx, csIdx);
  check('3.2b onclose 已绑定 handleSocketClose（重连唯一入口）',
    coFn.includes('ws.onclose = handleSocketClose'));
  check('3.3 录音中异常关闭 → scheduleReconnect',
    fn.includes('scheduleReconnect()') && fn.indexOf('!closedByClient && isRecording.value') > 0);
  check('3.4 跨会话文本不丢（committedText += sessionText）',
    fn.includes('committedText += sessionText'));
}

// ── 3b. 致命错误不得走重连（A4.9 R2-C1：fatal 标志必须被消费）──
{
  const errIdx = SRC.indexOf("payload.event === 'error'");
  const branch = SRC.slice(errIdx, errIdx + 1100);
  check('3.5 error 分支消费 payload.fatal（否则致命错误10轮 run-task 风暴）',
    branch.includes('payload.fatal'), '服务端发 fatal:true 但客户端不读＝死字段');
  check('3.6 致命分支不 return 而是拆会话',
    /if \(isRecording\.value && !payload\.fatal\)/.test(branch), branch.slice(0, 200));
  // 服务端契约同步核对（两侧行号无关，只核语义锚）
  const srv = fs.readFileSync(path.join(__dirname, '../../../server/app/services/asr_service.py'), 'utf8');
  check('3.7 服务端 task-failed 以 WS_CLOSE_FATAL 收尾',
    srv.includes('WS_CLOSE_FATAL, "asr task failed"') || srv.includes('WS_CLOSE_FATAL, "asr task failed"'.replace(/"/g, "'")));
  check('3.8 服务端上游掉线以 WS_CLOSE_UPSTREAM_DROP 收尾',
    srv.includes('WS_CLOSE_UPSTREAM_DROP') && srv.includes('code=WS_CLOSE_UPSTREAM_DROP'));
}

// ── 4. 重连有界 + 世代守卫（既有不变量不回退）──
{
  check('4.1 重连次数上限在位', /MAX_RECONNECT_ATTEMPTS\s*=\s*\d+/.test(SRC));
  check('4.2 指数退避在位', SRC.includes('RECONNECT_BASE_DELAY_MS') && SRC.includes('2 ** reconnectAttempt'));
  check('4.3 世代守卫（sessionEpoch）不回退', SRC.includes('if (epoch !== sessionEpoch)'));
  check('4.4 耗尽后清音频缓冲（防幽灵文本回放）',
    SRC.includes('pendingChunks = []') && SRC.includes('语音识别连接断开，重连失败'));
}

console.log(failed === 0 ? 'ALL PASS' : `FAILED ${failed}`);
process.exit(failed === 0 ? 0 : 1);
