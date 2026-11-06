/**
 * Edit-mode note reference restore (2026-11-04): editing a user message that
 * carried [note-ref] blocks stripped the markers from the editable text and
 * never restored them — the resent content replaced the DB row, permanently
 * losing the note reference and its inlined body (same loss class as the
 * 2026-10-09 [file-ref] wave; files were fixed, notes were not).
 *
 * Regression contract: parseNoteRefBlocks restores the blocks as note refs
 * (id|title header + snapshot body), buildNoteRefBlock re-emits an equivalent
 * block on resend (round-trip with ChatInput's format), stripNoteRefBlocks
 * removes blocks without touching the body text, and restoreEditNotes/
 * dropRestoredNotes mirror the fileRefs edit-session scoping (A4.9 I-1):
 * edit A → edit B replaces A's restored refs, manual refs survive, a normal
 * send drops restored refs.
 *
 * Run: node --experimental-strip-types tests/workflows/test_edit_note_ref_restore.cjs
 */
const { parseNoteRefBlocks, stripNoteRefBlocks, buildNoteRefBlock, restoreEditNotes, dropRestoredNotes } = require('../../src/utils/noteRefs.ts')

const cases = []

function check(name, ok) {
  cases.push([name, !!ok])
}

// 1. block round-trips through the send format (parse → build → parse, equal)
{
  const original = { id: 'note-1', title: '架构笔记', content: '第一段\n\n第二段 **加粗**' }
  const content = buildNoteRefBlock(original) + '帮我总结这个笔记'
  const refs = parseNoteRefBlocks(content)
  const r = refs[0] || {}
  check('block round-trips (id/title/content exact)', refs.length === 1
    && r.id === 'note-1' && r.title === '架构笔记' && r.content === original.content)
  const rebuilt = parseNoteRefBlocks(buildNoteRefBlock(r))
  check('re-emitted block parses back equal', rebuilt.length === 1
    && rebuilt[0].id === r.id && rebuilt[0].title === r.title && rebuilt[0].content === r.content)
}

// 2. multiple blocks restore in order; body text is not mixed into refs
{
  const content = buildNoteRefBlock({ id: 'a', title: '甲', content: 'A 正文' })
    + buildNoteRefBlock({ id: 'b', title: '乙', content: 'B 正文' })
    + '对比这两个笔记'
  const refs = parseNoteRefBlocks(content)
  check('multiple blocks restore in order', refs.length === 2
    && refs[0].id === 'a' && refs[1].id === 'b'
    && refs[0].content === 'A 正文' && refs[1].content === 'B 正文')
  check('body text preserved outside blocks', stripNoteRefBlocks(content).trim() === '对比这两个笔记')
}

// 3. title may contain `|` (header splits on the FIRST pipe; id is pipe-less)
{
  const refs = parseNoteRefBlocks(buildNoteRefBlock({ id: 'n7', title: 'A|B 计划', content: 'x' }))
  check('title with pipe survives', refs.length === 1 && refs[0].id === 'n7' && refs[0].title === 'A|B 计划')
}

// 4. multi-paragraph body (blank lines) survives the non-greedy body capture
{
  const body = '段一\n\n段二\n\n- 列表项\n\n段三'
  const refs = parseNoteRefBlocks(buildNoteRefBlock({ id: 'm', title: 't', content: body }) + '尾部问题')
  check('multi-paragraph body exact', refs.length === 1 && refs[0].content === body)
}

// 5. no blocks → no refs; strip is a no-op on plain text
{
  check('no blocks → no refs', parseNoteRefBlocks('普通文本 [note-ref:未闭合').length === 0
    && parseNoteRefBlocks('').length === 0)
  check('strip no-op on plain text', stripNoteRefBlocks('普通文本') === '普通文本')
}

// 6. malformed block (no `|` header) is skipped by parse but stripped from text
{
  const content = '[note-ref:broken]\n正文\n[/note-ref]\n\n可见文本'
  check('malformed block skipped by parse but stripped', parseNoteRefBlocks(content).length === 0
    && stripNoteRefBlocks(content).trim() === '可见文本')
}

// 7. restoreEditNotes scopes the edit session: previous restored refs are
//    REPLACED (edit A → edit B must not carry A's notes), manual refs are
//    kept, incoming refs are tagged restored
{
  const manual = { id: 'user-picked', title: '手动', content: '手动引用' }
  const staleRestored = { id: 'from-a', title: 'A 的', content: 'A', restored: true }
  const bRefs = parseNoteRefBlocks(buildNoteRefBlock({ id: 'from-b', title: 'B 的', content: 'B' }))
  const next = restoreEditNotes([manual, staleRestored], bRefs)
  check('restoreEditNotes replaces stale restored, keeps manual', next.length === 2
    && next[0].id === 'user-picked' && next[0].restored !== true
    && next[1].id === 'from-b' && next[1].restored === true
    && !next.some((n) => n.id === 'from-a'))
}

// 8. restoreEditNotes dedupes incoming against manual refs (same note both
//    manually referenced and referenced by the edited message → single ref,
//    manual status kept)
{
  const manual = { id: 'dup', title: '手动', content: '手动时拉取' }
  const incoming = parseNoteRefBlocks(buildNoteRefBlock({ id: 'dup', title: '消息里的', content: '快照' }))
  const next = restoreEditNotes([manual], incoming)
  check('restoreEditNotes dedupes against manual refs', next.length === 1
    && next[0].content === '手动时拉取' && next[0].restored !== true)
}

// 9. dropRestoredNotes strips restored-tagged refs from a normal (non-edit)
//    send, keeping manual refs (abandoned edit must not leak notes into an
//    unrelated message — mirrors dropRestoredChips)
{
  const notes = [
    { id: 'manual', title: '手动', content: 'm' },
    { id: 'restored', title: '恢复', content: 'r', restored: true },
  ]
  const kept = dropRestoredNotes(notes)
  check('dropRestoredNotes strips restored only', kept.length === 1 && kept[0].id === 'manual')
}

// 10. restoreEditNotes with EMPTY incoming purges stale restored refs (editing
//     a message with no note-refs must still clear the previous edit session's
//     refs — mirrors restoreEditChips' empty-incoming semantics)
{
  const manual = { id: 'manual', title: '手动', content: 'm' }
  const staleRestored = { id: 'stale', title: '旧', content: 's', restored: true }
  const next = restoreEditNotes([manual, staleRestored], [])
  check('empty incoming purges stale restored', next.length === 1 && next[0].id === 'manual')
}

// 11. edit → resend round-trip: edit mode sends every restored ref (the
//     filesToAttach/notesToSend ternary), re-encoding the same blocks; the
//     normal-send branch after an abandoned edit encodes none of them
{
  const sent = buildNoteRefBlock({ id: 'n1', title: 'T', content: 'C' })
    + buildNoteRefBlock({ id: 'n2', title: 'U', content: 'D' })
    + '原始正文'
  const restored = restoreEditNotes([], parseNoteRefBlocks(sent))
  let resent = ''
  for (const n of restored) resent += buildNoteRefBlock(n)  // editMessageId armed → all
  resent += '编辑后正文'
  const again = parseNoteRefBlocks(resent)
  check('edit→resend re-encodes refs equivalently', again.length === 2
    && again[0].id === 'n1' && again[0].content === 'C'
    && again[1].id === 'n2' && again[1].content === 'D'
    && stripNoteRefBlocks(resent).trim() === '编辑后正文')
  let abandoned = ''
  for (const n of dropRestoredNotes(restored)) abandoned += buildNoteRefBlock(n)  // normal send
  check('abandoned edit → normal send encodes no restored refs',
    parseNoteRefBlocks(abandoned + '无关正文').length === 0)
}

let fails = 0
for (const [name, ok] of cases) {
  if (!ok) fails += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`)
}
console.log(fails === 0 ? '\nall checks pass' : `\n${fails} FAILURES`)
process.exit(fails === 0 ? 0 : 1)
