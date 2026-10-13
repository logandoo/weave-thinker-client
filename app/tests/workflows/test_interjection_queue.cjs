/* agent 插话队列接线不变量（需求 4）。
 *
 * 症状（用户）：①流式回答中发送的插话 query 直接显示在"正在回复的 query 下方"
 * （消息列表里、流式回答之上），而不是作为队列显示在回答最下方；②上一问回答
 * 完毕后插话可能被静默丢弃。
 *
 * 本测试钉住三条**接线**不变量（AST，注释/定义不计入 —— 沿用
 * test_promote_wiring.cjs 范式），行为级证据由 Playwright 承担：
 *   Q1 乐观气泡不再写入 messages（显示位次的根因）
 *   Q2 resume 的 onDoneCallback 必须冲刷未确认插话（静默丢弃的最大来源）
 *   Q3 syncAfterAbort 终态必须冲刷
 *   Q4 去重闸门不得把本轮 query 自身当已 commit（否则兜底重发被跳过）
 *   Q5 兜底重发保留 assistantId
 *   Q6 ChatArea 的队列区渲染在 .streaming-message 之后
 *
 * Run: node tests/workflows/test_interjection_queue.cjs */
const fs = require('fs');
const path = require('path');
const ts = require('typescript');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const CHAT = path.resolve(__dirname, '..', '..', 'src', 'stores', 'chat.ts');
const AREA = path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatArea.vue');
const src = fs.readFileSync(CHAT, 'utf8');
const sf = ts.createSourceFile('chat.ts', src, ts.ScriptTarget.Latest, /* setParentNodes */ true, ts.ScriptKind.TS);

function functionByName(name) {
  let found = null;
  (function walk(node) {
    if (found) return;
    if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node) || ts.isVariableDeclaration(node)) && node.name && ts.isIdentifier(node.name) && node.name.text === name) {
      found = node;
      return;
    }
    ts.forEachChild(node, walk);
  })(sf);
  return found;
}
function bodyText(node) {
  return node ? node.getText(sf) : '';
}

// ── Q1 乐观气泡不得写入 messages ──────────────────────────────────────────
const sendInterjection = functionByName('sendInterjection');
check('Q0 sendInterjection 存在', !!sendInterjection);
const siText = bodyText(sendInterjection);
const pushesMessages = /messages\.value\[[^\]]*\]\s*\.push\(/.test(siText) ||
  /messages\.value\[[^\]]*\]\s*=\s*\[/.test(siText);
check('Q1 插话不再写入 messages（乐观气泡移出消息列表）', !pushesMessages);
check('Q1b 插话仍写入 pendingInterjections（队列唯一源）', /pendingInterjections\[/.test(siText));

// ── Q2 resume onDoneCallback 必须冲刷 ────────────────────────────────────
const onDoneCb = functionByName('onDoneCallback');
check('Q0b onDoneCallback 存在', !!onDoneCb);
const odText = bodyText(onDoneCb);
const odFlushes = /flushUnconfirmedInterjections\s*\(/.test(odText);
check('Q2 resume onDoneCallback 冲刷未确认插话', odFlushes);
// 冲刷必须在 endStreaming 之后（终态），且必须有 auto-send/restore 语义分流
const endIdx = odText.indexOf('endStreaming(');
const flushIdx = odText.search(/flushUnconfirmedInterjections\s*\(/);
check('Q2b 冲刷位于 endStreaming 之后', endIdx >= 0 && flushIdx > endIdx, { endIdx, flushIdx });

// ── Q3 syncAfterAbort 终态冲刷 ───────────────────────────────────────────
const syncAfterAbort = functionByName('syncAfterAbort');
check('Q0c syncAfterAbort 存在', !!syncAfterAbort);
const saText = bodyText(syncAfterAbort);
const saFlushCount = (saText.match(/flushUnconfirmedInterjections\s*\(/g) || []).length;
// 三个终态 return 各一次（Phase-1 计数命中 / Phase-2 轮询命中 / 尾部 local-abort），
// 少于 3 就说明还有终态把插话搁置（评审 A/B 同项发现）。
check('Q3 syncAfterAbort 每个终态 return 都冲刷', saFlushCount >= 3, { saFlushCount });

// ── Q4 去重闸门排除边界行自身 ────────────────────────────────────────────
const findCommitted = functionByName('_findCommittedInterjection');
check('Q0d _findCommittedInterjection 存在', !!findCommitted);
const fcText = bodyText(findCommitted);
check('Q4 去重扫描排除本轮自身 query 行（同文不误判已 commit）',
  /turnQueryIdx/.test(fcText));

// ── Q7 迟到的 done/error 不得拆掉后继 turn 的流（连接令牌）────────────────
// 插话兜底自动发送会立刻开启下一 turn；上一 run 的 done 随后才到是常态。无守卫
// 的 endStreaming 会把新 turn 的 abortController 置 null 并中止其 SSE —— 后端
// 已生成并落库回答，前端却收不到（实测 chatllm.log "Client disconnected during
// tool loop" + "Final answer already persisted by producer"）。
(function () {
  let rawDone = null;
  let rawError = null;
  (function walk(node) {
    if (ts.isPropertyAssignment(node) && ts.isIdentifier(node.name)) {
      if (node.name.text === 'onDone' && node.initializer && ts.isArrowFunction(node.initializer)) {
        if (node.initializer.getText(sf).includes('onFinalize')) rawDone = node.initializer;
      }
      if (node.name.text === 'onError' && node.initializer && ts.isArrowFunction(node.initializer)) {
        if (node.initializer.getText(sf).includes('currentError')) rawError = node.initializer;
      }
    }
    ts.forEachChild(node, walk);
  })(sf);
  check('Q7a 主流 onDone 存在连接令牌守卫', !!rawDone && /abortController\s*!==\s*connection/.test(rawDone.getText(sf)));
  check('Q7b 主流 onError 存在连接令牌守卫', !!rawError && /abortController\s*!==\s*connection/.test(rawError.getText(sf)));
})();

// ── Q8 refreshConversation 重挂前必须复查本地流（不得掐断刚开启的回答）────
// 实测根因：ChatArea watcher → refreshConversation → beginStreaming +
// resumeActiveStream → abortPredecessor 把插话兜底刚开启的下一 turn 掐断。
(function () {
  // 取实际做接管的函数（beginStreaming + resumeActiveStream 所在），不按名字猜。
  let owner = null;
  (function walk(node) {
    if (owner) return;
    if ((ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node)) && node.body) {
      const txt = node.getText(sf);
      if (/beginStreaming\s*\(/.test(txt) && /resumeActiveStream\s*\(/.test(txt) && /getStreamStatus\s*\(/.test(txt)) {
        owner = node;
        return;
      }
    }
    ts.forEachChild(node, walk);
  })(sf);
  check('Q8a 找到做接管的函数（selectConversation）', !!owner);
  if (owner) {
    const body = owner.getText(sf);
    const iStatus = body.indexOf('getStreamStatus');
    const iTakeover = body.indexOf('beginStreaming(');
    check('Q8b getStreamStatus 之后、接管之前有本地流复查',
      iStatus > 0 && iTakeover > iStatus && /_liveAfterStatus/.test(body.slice(iStatus, iTakeover)));
  } else {
    check('Q8b getStreamStatus 之后、接管之前有本地流复查', false);
  }
})();

// ── Q9 resume 不得接管健康的活连接（否则掐断下一 turn 的回答流）──────────
(function () {
  const fn = functionByName('resumeActiveStream');
  check('Q9a resumeActiveStream 存在', !!fn);
  if (!fn) { check('Q9b abortPredecessor 调用之前有活连接接管守卫', false); return; }
  // 用 AST 位置比较（注释里出现的同名单词不算）：
  // 守卫 IfStatement 的 pos 必须早于 abortPredecessor 调用表达式的 pos。
  let guardPos = -1, callPos = -1;
  (function walk(node) {
    if (ts.isIfStatement(node) && /abortController\.signal/.test(node.expression.getText(sf)) && guardPos < 0) {
      guardPos = node.pos;
    }
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 'abortPredecessor' && callPos < 0) {
      callPos = node.pos;
    }
    ts.forEachChild(node, walk);
  })(fn);
  check('Q9b abortPredecessor 调用之前有活连接接管守卫',
    guardPos >= 0 && callPos > guardPos, { guardPos, callPos });
})();

// ── Q10 tryReconnectOrSync 不得接管/收尾一条健康的活连接 ─────────────────
// 旧 turn 的 doStream 尾巴会带着旧的 expectedMessageCount 闯进恢复链，把
// 插话兜底刚开启的下一 turn 收尾掉（评审 A Critical #1）。
(function () {
  const fn = functionByName('tryReconnectOrSync');
  const body = fn ? fn.getText(sf) : '';
  const guardIdx = body.search(/abortController\.signal/);
  const recIdx = body.indexOf('_recoveryInFlight.add');
  check('Q10a tryReconnectOrSync 存在', !!fn);
  check('Q10b 进入恢复前有健康活连接让路守卫', guardIdx >= 0 && (recIdx < 0 || guardIdx < recIdx), { guardIdx, recIdx });
})();

// ── Q11 resume complete 两个分支都要冲刷（评审复审 A3 残留）─────────────
(function () {
  const fn = functionByName('resumeActiveStream');
  const body = fn ? fn.getText(sf) : '';
  const flushCount = (body.match(/flushUnconfirmedInterjections\s*\(/g) || []).length;
  check('Q11 resume complete 各分支均冲刷', flushCount >= 1, { flushCount });
})();

// ── Q5 兜底重发真正贯通 assistantId（存进条目 + 重发时透传）────────────
const flushFn = functionByName('flushUnconfirmedInterjections');
check('Q0e flushUnconfirmedInterjections 存在', !!flushFn);
const flText = bodyText(flushFn);
const sendFn = functionByName('sendInterjection');
const seText = bodyText(sendFn);
check('Q5a 兜底重发不硬编码 null', !/sendMessage\(\s*text\s*,\s*null\s*,/.test(flText));
check('Q5b assistantId 随插话条目落存（此前是死管道）', /assistantId/.test(seText));
check('Q5c 重发时按条目透传 assistantId', /assistantIds\[i\]/.test(flText));

// ── Q6 ChatArea 队列区在流式回答之后 ─────────────────────────────────────
const area = fs.readFileSync(AREA, 'utf8');
const streamIdx = area.indexOf('class="streaming-message"');
const queueIdx = area.indexOf('interjection-queue');
check('Q6a ChatArea 存在插话队列区', queueIdx > 0);
check('Q6b 队列区渲染在 .streaming-message 之后', streamIdx > 0 && queueIdx > streamIdx, { streamIdx, queueIdx });
check('Q6c 队列数据源为 pendingInterjections', /pendingInterjections/.test(area));

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
