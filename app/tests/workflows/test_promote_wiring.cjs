/* Structural regression for the busy-final rollback WIRING (A4.9 R3 Important).
 *
 * The pure rollback logic is covered behaviorally by
 * test_conversation_order.cjs (G1/G2). THIS test guards the CALL SITES in
 * stores/chat.ts via the TypeScript AST (comments/definitions excluded), so
 * deleting a call site, moving one into the retry-success branch, or dropping
 * the authoritative-refresh gate fails the suite.
 *
 * Proved by the revert-and-fail cycle (see verification_log): removing the
 * sendMessage call → W1/W2/W3/W4 red; moving it into the retry branch → W3 red;
 * commenting out the refresh → W4 red; restoring → all green.
 * Run: node tests/workflows/test_promote_wiring.cjs */
const fs = require('fs');
const path = require('path');
const ts = require('typescript');

let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

const SRC = path.resolve(__dirname, '..', '..', 'src', 'stores', 'chat.ts');
const src = fs.readFileSync(SRC, 'utf8');
const sf = ts.createSourceFile('chat.ts', src, ts.ScriptTarget.Latest, /* setParentNodes */ true, ts.ScriptKind.TS);

const rollbackCalls = [];
const recursiveSendCalls = [];
(function walk(node) {
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
    if (node.expression.text === 'rollbackPromotion') rollbackCalls.push(node);
    if (node.expression.text === 'sendMessage' &&
        node.arguments.some((a) => a.getText(sf).includes('_busyAttempts + 1'))) {
      recursiveSendCalls.push(node);
    }
  }
  ts.forEachChild(node, walk);
})(sf);

function enclosingFunctionName(node) {
  let p = node.parent;
  while (p) {
    if (ts.isFunctionDeclaration(p) && p.name) return p.name.text;
    if ((ts.isArrowFunction(p) || ts.isFunctionExpression(p)) && p.parent &&
        ts.isVariableDeclaration(p.parent) && ts.isIdentifier(p.parent.name)) {
      return p.parent.name.text;
    }
    p = p.parent;
  }
  return '(unknown)';
}
function enclosingIfs(node) {
  const out = [];
  let p = node.parent;
  while (p) {
    if (ts.isIfStatement(p)) out.push(p);
    p = p.parent;
  }
  return out;
}
const within = (node, container) => node.pos >= container.pos && node.end <= container.end;

// ── W1: call-site census (AST — comments and the definition are excluded) ───
// 2026-09-26 起 = 6：sendMessage/editAndResendMessage 的 busy-final ×2 +
// 两个 regenerate 函数各自的 busy 拒绝 + 通用 catch 回滚 ×2（A4.9 R1 I-1）。
check('W1 exactly 6 rollbackPromotion CALLS (busy-final ×2 + regenerate busy×2/catch×2; deletion/rename fails this)',
  rollbackCalls.length === 6, rollbackCalls.length);

function enclosingCatch(node) {
  let p = node.parent;
  while (p) {
    if (ts.isCatchClause(p)) return p;
    p = p.parent;
  }
  return null;
}

// ── W2–W4: per-site placement ───────────────────────────────────────────────
for (const call of rollbackCalls) {
  const fn = enclosingFunctionName(call);
  const ifs = enclosingIfs(call);

  const busyGuard = ifs.find((i) => i.expression.getText(sf).includes('busyRejected'));
  const catchClause = enclosingCatch(call);
  check(`W2 ${fn}: rollback guarded by a busyRejected branch OR a catch clause (generic-error path)`,
    !!busyGuard || !!catchClause,
    ifs.map((i) => i.expression.getText(sf).slice(0, 60)));

  const retryGuard = ifs.find((i) => i.expression.getText(sf).includes('_busyAttempts < 2'));
  const inRetryThen = retryGuard ? within(call, retryGuard.thenStatement) : false;
  check(`W3 ${fn}: rollback NOT inside the retry-success branch`, !inRetryThen);

  // The rollback must be gated by an authoritative refresh. Accepted shapes:
  //   if (await refreshConversation(x)) { rollback }        ← current
  //   await refreshConversation(x); rollback                ← legacy shape
  const stmt = call.parent;
  const block = stmt.parent;
  const isRefreshIf = (n) => ts.isIfStatement(n) &&
    n.expression.getText(sf).includes('await') &&
    n.expression.getText(sf).includes('refreshConversation');
  let refreshGated = false;
  if (ts.isBlock(block)) {
    const parentIf = ts.isIfStatement(block.parent) ? block.parent : null;
    if (parentIf && block === parentIf.thenStatement && isRefreshIf(parentIf)) {
      refreshGated = true;
    }
    const idx = block.statements.indexOf(stmt);
    if (!refreshGated && idx > 0) {
      const prev = block.statements[idx - 1];
      if (isRefreshIf(prev)) {
        refreshGated = within(call, prev.thenStatement);
      } else if (ts.isExpressionStatement(prev)) {
        let e = prev.expression;
        if (ts.isAwaitExpression(e)) e = e.expression;
        refreshGated = ts.isCallExpression(e) && ts.isIdentifier(e.expression) &&
          e.expression.text === 'refreshConversation';
      }
    }
  }
  check(`W4 ${fn}: rollback gated by "if (await refreshConversation(...))"`, refreshGated);
}

// ── P1–P2: promoteConversationToTop census (2026-09-26 重新生成置顶) ─────────
// 发送 / 编辑重发 / 重新生成×2 都是「最新操作」，必须全部乐观置顶；删掉任何
// 一个调用点（如新增一条 regenerate 路径忘记接线）即红。
const promoteCalls = [];
(function walk2(node) {
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
      node.expression.text === 'promoteConversationToTop') promoteCalls.push(node);
  ts.forEachChild(node, walk2);
})(sf);
const promoteFns = promoteCalls.map((n) => enclosingFunctionName(n)).sort();
check('P1 promoteConversationToTop call sites = 4 (sendMessage/editAndResendMessage/regenerateLastAssistantMessage/regenerateWithForceResults)',
  promoteCalls.length === 4, promoteFns);
for (const fn of ['sendMessage', 'editAndResendMessage',
                  'regenerateLastAssistantMessage', 'regenerateWithForceResults']) {
  check(`P2 ${fn}: calls promoteConversationToTop`, promoteFns.includes(fn));
}

// ── P3: onConversationBusy census (A4.9 R1 I-1：未接线=busy 拒绝永不回滚) ────
const busyProps = [];
(function walk3(node) {
  if (ts.isPropertyAssignment(node) && ts.isIdentifier(node.name) &&
      node.name.text === 'onConversationBusy') busyProps.push(node);
  ts.forEachChild(node, walk3);
})(sf);
const busyFns = busyProps.map((n) => enclosingFunctionName(n)).sort();
// sendMessage/editAndResendMessage 的回调包在 doStream/doEditStream 箭头函数里，
// enclosingFunctionName 解析到包装函数名。
check('P3 onConversationBusy wired in send(doStream)/edit(doEditStream)/regenerate×2 (=4)',
  busyProps.length === 4 &&
  ['doStream', 'doEditStream', 'regenerateLastAssistantMessage', 'regenerateWithForceResults']
    .every((f) => busyFns.includes(f)),
  busyFns);

// ── W5: anchor — the retry-success recursion must still exist ───────────────
check('W5 retry-success recursion call exists (branch-placement anchor)',
  recursiveSendCalls.length >= 1, recursiveSendCalls.length);

console.log(failed === 0 ? '\nall checks pass' : `\n${failed} check(s) FAILED`);
process.exit(failed === 0 ? 0 : 1);
