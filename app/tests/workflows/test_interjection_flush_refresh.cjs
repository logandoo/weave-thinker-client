/* W26 插话终态冲刷判重时序不变量（重复气泡根治）。
 *
 * 症状（用户）：插话只发送一次，run 结束后消息列表里同一插话文本的
 * query 气泡出现两次。
 *
 * 根因：flushUnconfirmedInterjections 在 onDone / resume onDoneCallback 中
 * 先于 refreshConversation 执行；commit-echo 不把服务器行插入 messages，
 * 去重门 _findCommittedInterjection 扫的是刷新前的本地列表 —— 服务端已
 * 落库的插话被判「未确认」→ sendMessage 再落一行 → 双气泡。
 * echo 丢失通道（subscriber drop-oldest / 断连期 replay 不带插话事件）放大。
 *
 * 本测试钉住的接线不变量（AST，沿用 test_interjection_queue.cjs 范式）：
 *   R1 auto-send 冲刷在 drain/判重之前先 await refreshConversation（权威快照）
 *   R2 刷新调用受 auto-send 模式守卫（restore 模式不刷新——文本只回填输入框）
 *   R3 刷新失败不阻断冲刷（宁发不丢语义保留：try/catch 包裹）
 *
 * Run: node tests/workflows/test_interjection_flush_refresh.cjs */
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
const src = fs.readFileSync(CHAT, 'utf8');
const sf = ts.createSourceFile('chat.ts', src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);

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

const flushFn = functionByName('flushUnconfirmedInterjections');
check('R0 flushUnconfirmedInterjections 存在', !!flushFn);
if (flushFn) {
  const body = flushFn.getText(sf);
  const refreshIdx = body.search(/await\s+refreshConversation\s*\(\s*convId\s*,\s*\{\s*reuseFresh:\s*true\s*\}\s*\)/);
  const drainIdx = body.indexOf('_drainUnconfirmedInterjections(');
  const gateIdx = body.indexOf('_findCommittedInterjection(');

  // R1 刷新必须先于 drain（出队）与判重门扫描；且必须 reuseFresh ——
  // A4.9 W26 评审 C1：裸 refresh 会被调用点紧随的竞对 refresh 以
  // refreshSeqs 最后写获胜作废（快照永不应用=修复失效）；reuseFresh 在
  // TTL 内复用调用方刚应用的权威快照，无竞对路径才真拉取。
  check('R1a auto-send 冲刷含 reuseFresh 刷新', refreshIdx >= 0, { refreshIdx });
  check('R1b 刷新先于 drain（判重门扫的是权威快照）',
    refreshIdx >= 0 && drainIdx > refreshIdx, { refreshIdx, drainIdx });
  check('R1c 刷新先于判重门 _findCommittedInterjection',
    refreshIdx >= 0 && gateIdx > refreshIdx, { refreshIdx, gateIdx });

  // R2 restore 模式（用户主动停止，文本回填输入框）不得触发刷新——
  // 刷新调用必须落在 mode==='auto-send' 守卫之内：guard 位置 < 刷新位置，
  // 且 restore 提前 return 位置在刷新之后。
  const autoSendGuard = body.search(/mode\s*===\s*'auto-send'/);
  const restoreReturn = body.search(/mode\s*===\s*'restore'/);
  check('R2 刷新受 auto-send 模式守卫（restore 不刷新）',
    autoSendGuard >= 0 && refreshIdx > autoSendGuard && restoreReturn >= 0 && restoreReturn > refreshIdx,
    { autoSendGuard, refreshIdx, restoreReturn });

  // R3 刷新失败不阻断（try/catch）——宁发不丢语义不退化
  check('R3 刷新调用被 try 包裹（失败走既有本地快照判重）',
    /try\s*\{[^}]*await\s+refreshConversation\s*\(\s*convId\s*,\s*\{\s*reuseFresh:\s*true\s*\}\s*\)/s.test(body));
}

// ── R4 调用点时序（评审 C1 根治面）：auto-send 冲刷绝不得与竞对 refresh 并行 ──
// R4a resume onDoneCallback：refresh 在 flush 之前（串行 IIFE）
const onDoneCb = functionByName('onDoneCallback');
check('R4a resume onDoneCallback 先 refresh 后 flush（串行）', (() => {
  if (!onDoneCb) return false;
  const t = onDoneCb.getText(sf);
  const r = t.search(/await\s+refreshConversation\s*\(/);
  const f = t.search(/await\s+flushUnconfirmedInterjections\s*\(/);
  return r >= 0 && f > r;
})());
// R4b 主流 onDone 回调体内不再冲刷（冲刷锚定 promise 结算后的 refresh 之后）
(function () {
  let rawDone = null;
  (function walk(node) {
    if (ts.isPropertyAssignment(node) && ts.isIdentifier(node.name)
        && node.name.text === 'onDone' && node.initializer
        && node.initializer.getText(sf).includes('onFinalize')) {
      rawDone = node.initializer;
    }
    ts.forEachChild(node, walk);
  })(sf);
  check('R4b 主流 onDone 体内不再直接冲刷（移至 refresh 后）',
    !!rawDone && !/flushUnconfirmedInterjections\s*\(/.test(rawDone.getText(sf)));
})();
// R4c 主发送流 promise 结算后：refresh 之后紧跟 flush（同一顺序对）
check('R4c 主流终态 refresh→flush 串行锚定', (() => {
  const m = src.match(/await\s+refreshConversation\s*\(\s*conversationId\s*\)\s*\n[\s\S]{0,400}?await\s+flushUnconfirmedInterjections\s*\(\s*conversationId\s*,\s*'auto-send'\s*\)/);
  return !!m;
})());
// R4d 三个后台 auto-send 调用点全部 refresh→flush 串行（resume onError /
// resume none / 主 onError）——并行 void flush + void refresh 形态绝迹
check('R4d 后台 auto-send 不再与竞对 refresh 并行', (() => {
  // 旧形态：void flushUnconfirmedInterjections(...'auto-send') 之后同一
  // 语句块内还有 void refreshConversation —— 全局扫描该并行形态
  const parallel = /void\s+flushUnconfirmedInterjections\s*\(\s*\w+\s*,\s*'auto-send'\s*\)\s*\n\s*void\s+refreshConversation/;
  const reverse = /void\s+refreshConversation\s*\(\s*\w+\s*\)\s*\n\s*void\s+flushUnconfirmedInterjections/;
  return !parallel.test(src) && !reverse.test(src);
})());
// R4e 其余三条终态流（重生成 / 强制检索重生成 / 编辑重发）同样
// refresh→flush 串行 —— onDone 是四条流共享的 wireStreamCallbacks，
// 冲刷只能锚在各自 promise 结算后的权威 refresh 之后（评审 A-I1/B-I1）。
check('R4e regenerate/force/edit 三流终态 refresh→flush 串行', (() => {
  for (const name of ['regenerateLastAssistantMessage', 'regenerateWithForceResults', 'editAndResendMessage']) {
    const fn = functionByName(name);
    if (!fn) return false;
    const t = fn.getText(sf);
    const r = t.search(/await\s+refreshConversation\s*\(\s*convId\s*\)\s*\n\s*await\s+flushUnconfirmedInterjections\s*\(\s*convId\s*,\s*'auto-send'\s*\)/);
    if (r < 0) return false;
  }
  return true;
})());

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
