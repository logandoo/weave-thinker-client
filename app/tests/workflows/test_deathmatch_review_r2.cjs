// A4.9 R2 structural pins for review-specific stop/scope/evidence invariants.
const fs = require('node:fs');
const path = require('node:path');

const chat = fs.readFileSync(path.resolve(__dirname, '../../src/stores/chat.ts'), 'utf8');
const verification = fs.readFileSync(path.resolve(__dirname, '../../../server/app/services/deathmatch_verification.py'), 'utf8');
const scope = fs.readFileSync(path.resolve(__dirname, '../../e2e/_archive/deathmatch_feedback_scope.spec.ts'), 'utf8');
const sidebar = fs.readFileSync(path.resolve(__dirname, '../../src/components/Sidebar.vue'), 'utf8');
const zen = fs.readFileSync(path.resolve(__dirname, '../../src/components/ZenHistoryDrawer.vue'), 'utf8');
let pass = 0, fail = 0;
function check(name, condition) {
  if (condition) { pass++; console.log(`PASS ${name}`); }
  else { fail++; console.log(`FAIL ${name}`); }
}

const pendingBranches = chat.match(/if \(_pendingDeathmatchStops\.has\(convId\)\) \{[\s\S]*?if \(convId === currentConversationId\.value\)/g) || [];
check('both answer success paths scope OFF writes to current conversation', pendingBranches.length >= 2);
check('stop acceptance reset per attempt', (chat.match(/stopAccepted = false/g) || []).length >= 2);
check('pending uses read-merge-write', chat.includes('_mutatePendingDeathmatchStops') && chat.includes('_readPendingDeathmatchStops()'));
check('multi-tab storage listener present', chat.includes("addEventListener('storage'"));
check('per-conversation storage keys avoid whole-array clobber', chat.includes('_pendingStopKey') && chat.includes('_PENDING_DEATHMATCH_KEY_PREFIX'));
check('send path has pending-stop final guard', chat.includes('pendingStop ? false') && chat.includes("pendingStop ? 'stop'"));
check('LWW records and session overlay protect storage failures', chat.includes('DeathmatchStopRecord') && chat.includes('_PENDING_DEATHMATCH_SESSION_PREFIX') && chat.includes('_isNewerDeathmatchStopRecord'));
check('every stream attempt recomputes pending state', /const doStream = \(\) => \{[\s\S]*?_readPendingDeathmatchStops\(\)[\s\S]*?return chatApi\.streamChat/.test(chat));
check('busy retry preserves originating conversation flags', chat.includes('initialDeathmatchAction') && chat.includes('useLiveConversationState'));
check('remote OFF aborts foreground and background cross-tab streams', chat.includes('void stopStreaming(id)'));
check('legacy IDs are a t=0 baseline under newer clear records', chat.includes('Legacy IDs are only a t=0 baseline') && !chat.includes('localStorage.removeItem(_PENDING_DEATHMATCH_LEGACY_KEY)'));
check('explicit declarations override requires_file=false', verification.includes('not _requires_file and not _declared'));
check('busy→500 regression exists', scope.includes('busy then HTTP 500 retry does not acknowledge stop'));
check('OFF A→B regression exists', scope.includes('OFF A then switch B does not overwrite B mode/action'));
check('multi-tab regression exists', scope.includes('multi-tab storage events merge OFF and acknowledgement'));
check('storage listener matches per-ID keys', chat.includes("event.key.startsWith(_PENDING_DEATHMATCH_KEY_PREFIX)"));
check('pending Set is initialized without const reassignment', !chat.includes('_pendingDeathmatchStops = _readPendingDeathmatchStops()'));
check('terminal pending state polled', (scope.match(/expect\.poll\(\(\) => pendingIds/g) || []).length >= 3);
check('ON A→ordinary B regression exists', scope.includes('ON in A never leaks into ordinary B during selection'));
check('older armed ON never bypasses selected server state', !chat.includes('const preserveStart ='));
function clearsBeforeEmptySelect(src) {
  const block = src.slice(src.indexOf('if (emptyConv) {'));
  const clear = block.indexOf('chatStore.deathmatchMode = false');
  const select = block.indexOf('await chatStore.selectConversation(emptyConv.id)');
  return clear >= 0 && select >= 0 && clear < select;
}
check('Sidebar/Zen empty reuse establishes new-task boundary before await', [sidebar, zen].every(clearsBeforeEmptySelect));

console.log(`\n=== ${pass} pass / ${fail} fail ===`);
if (fail) process.exit(1);
