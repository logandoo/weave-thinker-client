/* 文件预览分类器纯函数测试（Node 直跑，type stripping）。
 * 2026-09-19：预览/卡片/消息引用的类型判定单一来源。
 * Run: node tests/workflows/test_file_preview_kinds.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/composables/filePreview.ts');
  const { classifyFile, isPreviewableKind, formatSize, fileTypeLabel } = mod;

  // ── A: extension → kind ────────────────────────────────────────────────
  check('A1 image exts', ['png', 'JPG', 'jpeg', 'gif', 'webp', 'bmp', 'svg']
    .every(e => classifyFile(`x.${e}`) === 'image'));
  check('A2 office word', classifyFile('报告.docx') === 'word' && classifyFile('a.doc') === 'word');
  check('A3 office excel', classifyFile('a.xlsx') === 'excel' && classifyFile('a.xls') === 'excel' && classifyFile('a.csv') === 'excel');
  check('A4 office ppt', classifyFile('a.pptx') === 'ppt' && classifyFile('a.ppt') === 'ppt');
  check('A5 pdf', classifyFile('a.pdf') === 'pdf');
  check('A6 markdown', classifyFile('a.md') === 'markdown' && classifyFile('a.markdown') === 'markdown');
  check('A7 code', ['py', 'js', 'ts', 'json', 'sh', 'yaml', 'yml', 'html', 'css', 'sql', 'go', 'rs', 'java']
    .every(e => classifyFile(`x.${e}`) === 'code'));
  check('A8 text', classifyFile('a.txt') === 'text' && classifyFile('a.log') === 'text');
  check('A9 archive', classifyFile('a.zip') === 'archive');
  check('A10 unknown', classifyFile('a.xyz') === 'unknown' && classifyFile('noext') === 'unknown');

  // ── B: explicit backend type wins (folder / media) ─────────────────────
  check('B1 folder type', classifyFile('any', 'folder') === 'folder');
  check('B1b dir type (W19 S3 契约规范值)', classifyFile('any', 'dir') === 'folder');
  check('B2 backend type word', classifyFile('noext', 'word') === 'word');
  check('B3 backend type image', classifyFile('noext', 'image') === 'image');

  // ── C: previewability ─────────────────────────────────────────────────
  check('C1 previewable office', ['word', 'excel', 'ppt'].every(k => isPreviewableKind(k)));
  check('C2 previewable media/text', ['image', 'video', 'audio', 'pdf', 'markdown', 'code', 'text'].every(k => isPreviewableKind(k)));
  check('C3 unknown/archive/folder not previewable', ['unknown', 'archive', 'folder'].every(k => !isPreviewableKind(k)));

  // ── D: labels / size ──────────────────────────────────────────────────
  check('D1 label folder', fileTypeLabel('folder') === '文件夹');
  check('D2 label unknown', fileTypeLabel('unknown') === '文件');
  check('D3 formatSize', formatSize(500) === '500 B' && formatSize(2048) === '2.0 KB' && formatSize(3 * 1048576) === '3.0 MB');

  console.log(failed === 0 ? '\nall checks pass' : `\n${failed} checks FAILED`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
