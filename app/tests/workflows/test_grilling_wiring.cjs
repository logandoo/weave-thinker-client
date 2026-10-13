/* Structural regression for the grilling-questions reload WIRING.
 *
 * Bug (conv 7579bdc7, 2026-09-19): fetchGrillingQuestions() was defined and
 * exported but had ZERO call sites — opening/reloading a conversation stuck in
 * deathmatch grilling showed the status text but no question bubbles
 * (ChatArea.vue `isGrilling` requires grillingQuestions.length > 0).
 *
 * THIS test guards the CALL SITES in stores/chat.ts via the TypeScript AST
 * (comments and the definition are excluded), so deleting a call site, moving
 * one out of its conversation guard, or dropping the grilling-status gate
 * fails the suite.
 *
 * Proved by the revert-and-fail cycle (see verification_log): before the fix
 * W2/W3/W5 are red (0 call sites); after the fix all green.
 * Run: node tests/workflows/test_grilling_wiring.cjs */
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

let definitionCount = 0;
const callSites = [];
(function walk(node) {
  if (ts.isFunctionDeclaration(node) && node.name && node.name.text === 'fetchGrillingQuestions') {
    definitionCount += 1;
  }
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) &&
      node.expression.text === 'fetchGrillingQuestions') {
    callSites.push(node);
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

// ── W1: anchor — the definition must still exist (non-vacuous test) ─────────
check('W1 fetchGrillingQuestions definition exists', definitionCount === 1, definitionCount);

// ── W2/W3/W6: all three call sites wired ────────────────────────────────────
const inRefresh = callSites.filter((c) => enclosingFunctionName(c) === 'refreshConversation');
const inPoll = callSites.filter((c) => enclosingFunctionName(c) === 'pollDeathmatchStatus');
const inSelect = callSites.filter((c) => enclosingFunctionName(c) === 'selectConversation');
check('W2 refreshConversation calls fetchGrillingQuestions (reload/open path)', inRefresh.length >= 1,
  callSites.map((c) => enclosingFunctionName(c)));
check('W3 pollDeathmatchStatus calls fetchGrillingQuestions (out-of-band status path)', inPoll.length >= 1,
  callSites.map((c) => enclosingFunctionName(c)));
check('W6 selectConversation calls fetchGrillingQuestions (in-app switch-back path)', inSelect.length >= 1,
  callSites.map((c) => enclosingFunctionName(c)));

// ── W4: every call site is guarded by a grilling-status branch ──────────────
for (const call of callSites) {
  const fn = enclosingFunctionName(call);
  const ifs = enclosingIfs(call);
  const grillingGuard = ifs.find((i) => i.expression.getText(sf).includes('grilling'));
  check(`W4 ${fn}: call guarded by a grilling branch`, !!grillingGuard,
    ifs.map((i) => i.expression.getText(sf).slice(0, 80)));
}

// ── W5: call-site census (deletion/duplication fails this) ──────────────────
check('W5 exactly 3 fetchGrillingQuestions CALLS (refresh + poll + select)', callSites.length === 3, callSites.length);

console.log(failed === 0 ? '\nall checks pass' : `\n${failed} check(s) FAILED`);
process.exit(failed === 0 ? 0 : 1);
