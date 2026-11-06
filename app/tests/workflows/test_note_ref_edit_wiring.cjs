/**
 * Edit-mode note reference restore — ChatInput/ChatArea wiring invariants
 * (2026-11-04). The pure contract lives in test_edit_note_ref_restore.cjs;
 * this pins the Vue wiring so the restore cannot be silently disconnected:
 *
 *  - ChatArea.handleEdit parses [note-ref] blocks BEFORE stripping them and
 *    hands them to setEditContent (the 2026-11-04 bug: strips without
 *    restore → the note reference is lost on resend).
 *  - ChatInput.setEditContent re-seats the refs (restoreEditNotes) and
 *    materializes .note-tag chips in the editor DOM — syncInputText() drops
 *    any ref without a DOM tag, so state-only restores vanish on send.
 *  - loadDraftIntoInput does the same (draft refs were state-only → silently
 *    dropped by the DOM sync on the first keystroke/send).
 *  - handleSend: edit mode sends all refs, a normal send drops restored ones
 *    (dropRestoredNotes), and both send paths build blocks via
 *    buildNoteRefBlock (single wire-format source → guaranteed round-trip).
 *  - Interject does not silently swallow refs (guarded like attachments).
 *  - The edit arming does not travel across conversation switches.
 *
 * Run: node tests/workflows/test_note_ref_edit_wiring.cjs
 */
const fs = require('fs');
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

const inputSrc = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatInput.vue'), 'utf8');
const areaSrc = fs.readFileSync(path.resolve(__dirname, '..', '..', 'src', 'components', 'ChatArea.vue'), 'utf8');

// ── ChatArea.handleEdit ──────────────────────────────────────────────────
{
  const m = areaSrc.match(/function handleEdit\([\s\S]*?\n\}/);
  const fn = m ? m[0] : '';
  check('T1 handleEdit parses note-ref blocks', /parseNoteRefBlocks\(content\)/.test(fn), { fn: fn.slice(0, 400) });
  // parse must come BEFORE the stripping so the blocks still exist to parse
  const pi = fn.indexOf('parseNoteRefBlocks');
  const si = fn.indexOf('stripDsmlTags');
  check('T2 parse runs before the strip', pi > -1 && si > -1 && pi < si);
  check('T3 handleEdit passes refs to setEditContent', /setEditContent\(\s*strippedContent,\s*messageId,\s*restoredNotes\s*\)/.test(fn), { fn: fn.slice(-300) });
  // file chips restore still wired (question-2 regression face)
  check('T4 file chip restore unchanged (parseFileRefBlocks + restoreEditChips)',
    /parseFileRefBlocks\(content\)/.test(fn) && /restoreEditChips\(uploadStore\.files,\s*restoredChips\)/.test(fn));
}

// ── ChatInput.setEditContent ─────────────────────────────────────────────
{
  const m = inputSrc.match(/function setEditContent\([\s\S]*?\n\}/);
  const fn = m ? m[0] : '';
  check('T5 setEditContent accepts noteRefs', /function setEditContent\(\s*text:\s*string,\s*messageId:\s*string,\s*noteRefs\?/.test(fn), { head: fn.slice(0, 120) });
  check('T6 re-seats refs via restoreEditNotes', /restoreEditNotes\(\s*referencedNotes\.value,\s*noteRefs \?\? \[\]\s*\)/.test(fn));
  check('T7 materializes note tags in the editor DOM', /insertNoteTagElements\(\s*referencedNotes\.value\s*\)/.test(fn)
    && /editorRef\.value\.textContent = text/.test(fn));
}

// ── DOM sync survival (tags must exist or syncInputText drops the ref) ───
{
  const sync = inputSrc.match(/function syncInputText\(\)[\s\S]*?\n\}/);
  check('T8 syncInputText still drops tag-less refs (tag restore is load-bearing)',
    sync && /querySelectorAll\('\.note-tag'\)/.test(sync[0]));
  const draft = inputSrc.match(/function loadDraftIntoInput\([\s\S]*?\n\}/);
  check('T9 loadDraftIntoInput inserts note tags', draft && /insertNoteTagElements\(referencedNotes\.value\)/.test(draft[0]));
}

// ── handleSend branches ──────────────────────────────────────────────────
{
  const send = inputSrc.match(/async function handleSend\(\)[\s\S]*?\n  emit\('sent'\)/);
  const fn = send ? send[0] : '';
  check('T10 edit mode sends all refs / normal send drops restored',
    /editMessageId\.value \? referencedNotes\.value : dropRestoredNotes\(referencedNotes\.value\)/.test(fn));
  check('T11 note blocks built via buildNoteRefBlock (single wire source)',
    /buildNoteRefBlock\(noteRef\)/.test(fn) && !/\\n\[\/note-ref\]/.test(fn));
  check('T12 interject guard covers note refs (no silent swallow)',
    /dropRestoredNotes\(referencedNotes\.value\)\.length > 0\) return/.test(fn));
  const draftSend = inputSrc.match(/async function sendDraftNow\([\s\S]*?\n\}/);
  check('T13 sendDraftNow shares buildNoteRefBlock', draftSend && /buildNoteRefBlock\(noteRef\)/.test(draftSend[0]));
}

// ── edit arming lifecycle ────────────────────────────────────────────────
{
  check('T14 edit arming does not travel across conversations',
    /watch\(\(\) => chatStore\.currentConversationId,[\s\S]{0,200}?editMessageId\.value = null/.test(inputSrc));
  // review R2 F2: disarm must purge restored refs/chips immediately (not at
  // send) — otherwise the "chips visible yet unreferenced" symptom survives
  const disarm = inputSrc.match(/watch\(\(\) => chatStore\.currentConversationId[\s\S]*?\n\}\)/);
  check('T15 disarm purges restored refs + chips + DOM tags',
    disarm && /restoreEditNotes\(referencedNotes\.value, \[\]\)/.test(disarm[0])
      && /restoreEditChips\(uploadStore\.files, \[\]\)/.test(disarm[0])
      && /removeStaleNoteTagElements\(\)/.test(disarm[0]));
  // review R2 F5: the interjection backfill wipes the editor DOM — it must
  // re-materialize note tags or syncInputText drops the refs
  const refill = inputSrc.match(/watch\(\(\) => chatStore\.interjectionRestoreText[\s\S]*?\n\}\)/);
  check('T16 interjection backfill re-inserts note tags',
    refill && /insertNoteTagElements\(referencedNotes\.value\)/.test(refill[0]));
}

console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
if (failed) { console.log('RESULT: FAILED'); process.exit(1); }
console.log('RESULT: ALL CHECKS PASS');
