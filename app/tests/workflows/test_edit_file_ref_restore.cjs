/**
 * Edit-mode file chip restore (2026-10-09): editing a user message that
 * carried [file-ref] upload blocks stripped the markers from the editable
 * text and never restored them — the resent content replaced the DB row,
 * permanently losing the file cards (and the backend `[file-ref:` skill
 * trigger). Regression contract: parseFileRefBlocks restores the blocks as
 * upload chips (new 工作区路径 + legacy 文件路径 shapes, size text parsed
 * back to bytes) and mergeFileRefChips dedupes by workspace path; the chips
 * re-emit an equivalent block on resend (round-trip with ChatInput's format).
 *
 * Run: node --experimental-strip-types tests/workflows/test_edit_file_ref_restore.cjs
 */
const { parseFileRefBlocks, parseFileSizeText, mergeFileRefChips, restoreEditChips, dropRestoredChips } = require('../../src/utils/fileRefs.ts')

const cases = []

function buildBlock(f) {
  // Mirror ChatInput.handleSend's block format exactly.
  const sizeStr = f.size
    ? f.size < 1024 ? `${f.size} B` : f.size < 1048576 ? `${(f.size / 1024).toFixed(1)} KB` : `${(f.size / 1048576).toFixed(1)} MB`
    : '未知'
  return `[file-ref:${f.filename}]\n工作区路径: ${f.rel_path || f.file_path}\n文件类型: ${f.file_type || 'unknown'}\n文件大小: ${sizeStr}\n[/file-ref]\n\n`
}

// 1. new-format block parses to an equivalent chip (round-trip with the send format)
{
  const original = { filename: '报告.docx', rel_path: 'uploads/报告.docx', file_path: '/ws/uploads/报告.docx', file_type: 'docx', size: 2048 }
  const content = buildBlock(original) + '帮我分析这个文件'
  const refs = parseFileRefBlocks(content)
  const r = refs[0] || {}
  cases.push(['new-format block round-trips', refs.length === 1 && r.filename === '报告.docx'
    && r.rel_path === 'uploads/报告.docx' && r.file_type === 'docx' && r.size === 2048 && r.success === true])
}
// 2. legacy absolute-path block (文件路径 label) still parses
{
  const content = '[file-ref:old.txt]\n文件路径: /ws/uploads/old.txt\n文件类型: txt\n文件大小: 512 B\n[/file-ref]\n\n正文'
  const refs = parseFileRefBlocks(content)
  const r = refs[0] || {}
  cases.push(['legacy 文件路径 block parses', refs.length === 1 && r.file_path === '/ws/uploads/old.txt' && r.size === 512])
}
// 3. multiple blocks all restore, in order
{
  const content = buildBlock({ filename: 'a.md', rel_path: 'uploads/a.md', file_type: 'md', size: 100 })
    + buildBlock({ filename: 'b.xlsx', rel_path: 'uploads/b.xlsx', file_type: 'xlsx', size: 3145728 })
    + '两个问题'
  const refs = parseFileRefBlocks(content)
  cases.push(['multiple blocks restore in order', refs.length === 2 && refs[0].filename === 'a.md' && refs[1].filename === 'b.xlsx'
    && refs[1].size === 3145728])
}
// 4. KB / MB size text parses back to bytes (display-stable: 1.2 KB → 1229 → re-formats to 1.2 KB)
{
  cases.push(['size text variants', parseFileSizeText('123 B') === 123
    && parseFileSizeText('1.2 KB') === 1229 && parseFileSizeText('3.4 MB') === 3565158
    && parseFileSizeText('未知') === undefined && parseFileSizeText('') === undefined])
}
// 5. content without blocks yields no chips
{
  cases.push(['no blocks → no chips', parseFileRefBlocks('普通文本 [file-ref:未闭合').length === 0
    && parseFileRefBlocks('').length === 0])
}
// 6. merge dedupes by workspace path (re-uploaded same file not doubled); path-less chips kept
{
  const existing = [{ success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, file_type: 'md', size: 100 }]
  const incoming = [
    { success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, file_type: 'md', size: 100 },
    { success: true, filename: 'c.png', rel_path: 'uploads/c.png', file_path: null, file_type: 'png', size: 10 },
  ]
  const merged = mergeFileRefChips(existing, incoming)
  cases.push(['merge dedupes by path', merged.length === 2 && merged[1].filename === 'c.png'])
}
// 7. restored chip re-emits an equivalent block via the send format (resend keeps file cards)
{
  const content = buildBlock({ filename: '数据.csv', rel_path: 'uploads/数据.csv', file_type: 'csv', size: 1536 })
  const chip = parseFileRefBlocks(content)[0]
  const reemitted = chip ? buildBlock(chip) : ''
  const refs2 = parseFileRefBlocks(reemitted)
  cases.push(['restored chip re-emits parseable block', !!chip && refs2.length === 1 && refs2[0].filename === '数据.csv'
    && refs2[0].rel_path === 'uploads/数据.csv' && refs2[0].size === 1536])
}
// 8. restoreEditChips scopes the edit session: previous restored chips are
//    REPLACED (edit A → edit B must not attach A's files), genuine pending
//    uploads are kept, incoming chips are tagged restored (A4.9 I-1)
{
  const genuine = { success: true, filename: 'new.png', rel_path: 'uploads/new.png', file_path: null, file_type: 'png', size: 10 }
  const staleRestored = { success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, file_type: 'md', size: 100, restored: true }
  const bChips = parseFileRefBlocks(buildBlock({ filename: 'b.xlsx', rel_path: 'uploads/b.xlsx', file_type: 'xlsx', size: 2048 }))
  const next = restoreEditChips([genuine, staleRestored], bChips)
  cases.push(['restoreEditChips replaces stale restored, keeps genuine',
    next.length === 2 && next[0].filename === 'new.png' && next[1].filename === 'b.xlsx'
    && next[1].restored === true && !next.some((f) => f.filename === 'a.md')])
}
// 9. restoreEditChips dedupes incoming against genuine pending (same file both
//    freshly uploaded and referenced by the edited message → single chip)
{
  const genuine = { success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, file_type: 'md', size: 100 }
  const incoming = parseFileRefBlocks(buildBlock({ filename: 'a.md', rel_path: 'uploads/a.md', file_type: 'md', size: 100 }))
  const next = restoreEditChips([genuine], incoming)
  cases.push(['restoreEditChips dedupes against genuine pending', next.length === 1 && next[0].restored !== true])
}
// 10. dropRestoredChips strips restored-tagged chips from a normal (non-edit)
//     send, keeping genuine pending uploads (abandoned edit must not leak
//     attachments into an unrelated message — A4.9 I-1)
{
  const files = [
    { success: true, filename: 'new.png', rel_path: 'uploads/new.png', file_path: null },
    { success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, restored: true },
  ]
  const kept = dropRestoredChips(files)
  cases.push(['dropRestoredChips strips restored only', kept.length === 1 && kept[0].filename === 'new.png'])
}
// 11. restoreEditChips with EMPTY incoming purges stale restored chips (editing
//     a message with no file-refs must still clear the previous edit session's
//     chips — re-review round 2 residual), keeping genuine pending uploads
{
  const genuine = { success: true, filename: 'new.png', rel_path: 'uploads/new.png', file_path: null }
  const staleRestored = { success: true, filename: 'a.md', rel_path: 'uploads/a.md', file_path: null, restored: true }
  const next = restoreEditChips([genuine, staleRestored], [])
  cases.push(['empty incoming purges stale restored', next.length === 1 && next[0].filename === 'new.png'])
}

let fails = 0
for (const [name, ok] of cases) {
  if (!ok) fails += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`)
}
console.log(fails === 0 ? '\nall checks pass' : `\n${fails} FAILURES`)
process.exit(fails === 0 ? 0 : 1)
