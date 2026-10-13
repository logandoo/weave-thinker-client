/* resume_stream drain 503 瞬时重试（UPSTREAM_TODO_20261006 项 10，评审 B m6）。
 * part A: nextRetryAfterMs / classifyResumeFailure 纯函数
 * part B: fetchWithDrainRetry 有界重试行为（假 fetch/sleep）
 * part C: chat.ts + stores/chat.ts 接线断言（503 耗尽→轻提示 onTransient，
 *         不落红硬错 onError；store 侧 return false 交还有界恢复）
 * Run: node --experimental-strip-types tests/workflows/test_resume_503_retry.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

const mkResp = (status, retryAfter) => ({
  ok: status >= 200 && status < 300,
  status,
  headers: { get: (n) => (n.toLowerCase() === 'retry-after' ? (retryAfter ?? null) : null) },
});

async function main() {
  const { nextRetryAfterMs, fetchWithDrainRetry, classifyResumeFailure, sleepWithAbort, DRAIN_MAX_RETRIES } =
    await import('../../src/api/streamDispatch.ts');

  // ── part A: 纯函数 ────────────────────────────────────────────────────────
  check('A1 503 retry-after 5s → 5000ms', nextRetryAfterMs(503, '5') === 5000);
  check('A2 503 无头默认 5s', nextRetryAfterMs(503, null) === 5000);
  check('A2b Retry-After: 0 有下限 1s（防排空期紧打）', nextRetryAfterMs(503, '0') === 1000);
  check('A3 非 503 不重试', nextRetryAfterMs(404, '5') === null);
  check('A4 503 → transient-drain（轻提示面）', classifyResumeFailure(503) === 'transient-drain');
  check('A5 404 → skip', classifyResumeFailure(404) === 'skip');
  check('A6 401 → auth-expired', classifyResumeFailure(401) === 'auth-expired');
  check('A7 500 → error', classifyResumeFailure(500) === 'error');
  check('A8 429 → transient-retry（限流）', classifyResumeFailure(429) === 'transient-retry');
  check('A9 502/504 → transient-retry（网关）',
    classifyResumeFailure(502) === 'transient-retry' && classifyResumeFailure(504) === 'transient-retry');
  check('A10 429/502/504 可退避', nextRetryAfterMs(429, '3') === 3000
    && nextRetryAfterMs(502, null) === 5000 && nextRetryAfterMs(504, null) === 5000);

  // ── part B: 有界重试 ──────────────────────────────────────────────────────
  {
    const seq = [mkResp(503, '5'), mkResp(503, '2'), mkResp(200, null)];
    const calls = [];
    const sleeps = [];
    const resp = await fetchWithDrainRetry(
      async () => { calls.push(1); return seq[calls.length - 1] },
      '/u', {}, async (ms) => { sleeps.push(ms) });
    check('B1 503×2 后 200：重试 2 次得 200', resp.status === 200 && calls.length === 3);
    check('B2 退避按 Retry-After 序', String(sleeps) === '5000,2000');
  }
  {
    const calls = [];
    const sleeps = [];
    const resp = await fetchWithDrainRetry(
      async () => { calls.push(1); return mkResp(503, '1') },
      '/u', {}, async (ms) => { sleeps.push(ms) });
    check('B3 503 恒真：有界 1+MAX 后交轻路径', calls.length === 1 + DRAIN_MAX_RETRIES && resp.status === 503);
    check('B4 退避次数=MAX', sleeps.length === DRAIN_MAX_RETRIES);
  }
  {
    const calls = [];
    const resp = await fetchWithDrainRetry(
      async () => { calls.push(1); return mkResp(500, '5') },
      '/u', {}, async () => {});
    check('B5 非 503 不重试', calls.length === 1 && resp.status === 500);
  }

  // ── part B3: 429 按 Retry-After 节流重试（挂账清理波 criterion 3）───────────
  {
    const seq = [mkResp(429, '2'), mkResp(200, null)];
    const calls = [];
    const sleeps = [];
    const resp = await fetchWithDrainRetry(
      async () => { calls.push(1); return seq[calls.length - 1] },
      '/u', {}, async (ms) => { sleeps.push(ms) });
    check('B8 429 按 Retry-After 重试得 200', resp.status === 200 && calls.length === 2
      && String(sleeps) === '2000');
  }

  // ── part B2: 中断可醒（评审 A-5）──退避睡眠不得无视 AbortSignal ─────────────
  {
    const ac = new AbortController()
    const t0 = Date.now()
    const p = sleepWithAbort(30000, ac.signal)
    ac.abort()
    await p
    check('B6 abort 即刻醒（不睡满 30s）', Date.now() - t0 < 1000)
  }
  {
    // fetchWithDrainRetry 默认 sleepFn 带上 init.signal（resume 的 AbortSignal）
    const ac = new AbortController()
    const calls = []
    const t0 = Date.now()
    const p = fetchWithDrainRetry(
      async () => { calls.push(1); return mkResp(503, '30') },
      '/u', { signal: ac.signal })
    setTimeout(() => ac.abort(), 50)
    await p
    check('B7 重试退避随连接中止提前收束', calls.length === 1 + DRAIN_MAX_RETRIES
      && Date.now() - t0 < 25000)
  }

  // ── part C: 接线断言（同 test_stream_connection 源检风格） ──────────────────
  const chatSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'api', 'chat.ts'), 'utf-8');
  check('C1 resumeStream 走 fetchWithDrainRetry', chatSrc.includes('fetchWithDrainRetry('));
  const apiBranch = chatSrc.match(/transient-retry'\) \{([\s\S]*?)\n      \}/);
  check('C2 瞬时类耗尽分支走 onTransient（带因由）而非 onError',
    !!apiBranch && apiBranch[1].includes('onTransient') && apiBranch[1].includes("'drain'")
    && apiBranch[1].includes("'gateway'") && !apiBranch[1].includes('onError('));
  const storeSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chat.ts'), 'utf-8');
  const fnStart = storeSrc.indexOf('async function resumeActiveStream');
  const fnBody = storeSrc.slice(fnStart, storeSrc.indexOf('\n  }', fnStart + 100) + 4);
  check('C3 store 接 onTransient 标志', fnBody.includes('onTransient'));
  check('C4 瞬时暂态 return \'transient\'（交还有界恢复，不 endStreaming）',
    /if \(s\.transientReason !== null\) return 'transient'/.test(fnBody)
    && !/if \(s\.transientReason !== null\)[\s\S]{0,80}?endStreaming/.test(fnBody));
  // T2（挂账清零波）：因由按会话（StreamState 字段）——不得再有 store 级共享旗标
  check('C4b transientReason 按会话不共享',
    !/let transientReason/.test(storeSrc) && storeSrc.includes('transientReason: string | null')
    && storeSrc.includes('s.transientReason = reason'));
  // C7（mini 复审 Minor-2）：提示/错误块必须在 message-list 收口之后恒挂载
  // （不得回嵌 empty-state 的 v-else——busy 回滚空态曾使提示整区卸载不可见）
  {
    const chatArea = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'ChatArea.vue'), 'utf-8');
    const listClose = chatArea.indexOf('</div>', chatArea.indexOf('class="message-list"'));
    const noticePos = chatArea.indexOf('class="soft-notice"');
    const elseClose = chatArea.indexOf('      </template>\n    </div>');
    check('C7 提示块恒挂载（在 v-else 区外）',
      noticePos > listClose && (elseClose < 0 || noticePos > elseClose));
  }
  check('C5 轻提示面 currentNotice 在店且暴露（非 currentError 红面）',
    storeSrc.includes('const currentNotice = ref<string | null>(null)')
    && /return \{[\s\S]*?currentNotice,[\s\S]*?\}/.test(storeSrc.slice(storeSrc.lastIndexOf('return {'))));
  const storeDrainBranch = storeSrc.match(/attached === 'transient'\)\s*\{([\s\S]*?)\}/);
  check('C6 busy-attach 收 transient 走轻提示不落红错',
    !!storeDrainBranch && storeDrainBranch[1].includes('transientHintFor(s.transientReason)')
      && !storeDrainBranch[1].includes('currentError('));
  // C9（文案单源化后）：store 必须消费 transientHintFor（行为由 D1-D4 覆盖）
  check('C9 store 消费 transientHintFor 单源文案', storeSrc.includes('transientHintFor(s.transientReason)'));
  // C10（复审 B Minor-5）：429→busy 映射必须在场（否则删支路仍全绿）
  check('C10 429→busy 因由映射钉',
    /response\.status === 429 \? 'busy'/.test(chatSrc));

  // C8（挂账清理波）：轻提示须与红错成对复位——任何 currentError 复位点不得留旧提示
  {
    const errClears = (storeSrc.match(/currentError\.value = null/g) || []).length;
    const noticeClears = (storeSrc.match(/currentNotice\.value = null/g) || []).length;
    check('C8 提示与错误成对复位', errClears > 0 && errClears === noticeClears);
    const visSrc = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'stores', 'chatVisibility.ts'), 'utf-8');
    check('C8b visibility 复位成对', visSrc.includes('store.currentError = null')
      && visSrc.includes('store.currentNotice = null'));
  }

  // ── part D: 因由文案纯函数（复审 A Important-3 行为腿）────────────────────────
  {
    const { transientHintFor } = await import('../../src/api/streamDispatch.ts');
    check('D1 drain→排空文案', (transientHintFor('drain') || '').includes('排空'));
    check('D2 busy→限流文案', (transientHintFor('busy') || '').includes('限流'));
    check('D3 gateway→网关文案', (transientHintFor('gateway') || '').includes('网关'));
    check('D4 null/未知→网关兜底', transientHintFor(null) === transientHintFor('gateway')
      && (transientHintFor('zzz') || '').includes('网关'));
  }

  if (failed) {
    console.error(`\n${failed} check(s) FAILED`);
    process.exit(1);
  }
  console.log('\nall checks pass');
}

main().catch(e => { console.error(e); process.exit(1); });
