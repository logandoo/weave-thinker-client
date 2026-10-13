/* Note word-count pure function tests (T4 2026-09-11).
 * Mirrors backend app/tools/word_count.py semantics:
 *   word_count = CJK chars + latin words; char_count; no-spaces; lines; paragraphs.
 * Run: node --experimental-strip-types tests/workflows/test_note_word_count.cjs */
const fs = require('fs');
const path = require('path');

let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/utils/noteWordCount.ts');
  const { countNoteText } = mod;

  // ── A: CJK-aware word count ──────────────────────────────────────────
  const empty = countNoteText('');
  check('A1 empty → zeros', empty.wordCount === 0 && empty.charCount === 0 && empty.lineCount === 0 && empty.paragraphCount === 0, empty);

  const cjk = countNoteText('你好世界');
  check('A2 pure CJK 4 chars', cjk.wordCount === 4 && cjk.cjkChars === 4 && cjk.latinWords === 0, cjk);

  const mixed = countNoteText('你好世界 hello world');
  check('A3 mixed CJK+EN = 6', mixed.wordCount === 6 && mixed.cjkChars === 4 && mixed.latinWords === 2, mixed);
  check('A3b charCount includes space', mixed.charCount === '你好世界 hello world'.length, mixed);

  const cjkPunct = countNoteText('你好，世界！');
  check('A4 CJK punctuation counts as CJK chars', cjkPunct.cjkChars === 6 && cjkPunct.wordCount === 6, cjkPunct);

  const punct = countNoteText('hello, world!');
  check('A5 EN punctuation stays 2 words', punct.latinWords === 2 && punct.wordCount === 2, punct);

  const numbers = countNoteText('123 456');
  check('A6 numbers count as 2 latin words', numbers.latinWords === 2, numbers);

  // ── B: chars / spaces / lines / paragraphs ───────────────────────────
  const spacing = countNoteText('a b\tc\nd');
  check('B1 no-spaces strips space/tab/newline', spacing.charCountNoSpaces === 4, spacing);

  const lines = countNoteText('a\nb\nc');
  check('B2 three lines, three paragraphs', lines.lineCount === 3 && lines.paragraphCount === 3, lines);

  const blankLines = countNoteText('a\n\nb');
  check('B3 blank line counted in lines, not in paragraphs', blankLines.lineCount === 3 && blankLines.paragraphCount === 2, blankLines);

  const onlyNewline = countNoteText('\n');
  check('B4 single newline → 2 lines', onlyNewline.lineCount === 2, onlyNewline);

  // ── C: wiring assertions (shared WordCountButton + NoteEditor) ───────
  const editor = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'NoteEditor.vue'), 'utf-8');
  const wcBtn = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'WordCountButton.vue'), 'utf-8');
  check('C1 shared component owns the button + popover', wcBtn.includes('word-count-btn') && wcBtn.includes('word-count-teleport'));
  check('C2 popover renders the four stats', ['字数', '字符数', '行数', '段落'].every(k => wcBtn.includes(k)));
  check('C3 spacer pushes button to the far right', editor.includes('toolbar-spacer'));
  check('C4 editor wires shared button with visible-text getter', editor.includes('WordCountButton') && editor.includes('get-text="wordCountSourceText"') && editor.includes('visibleEditorText'));
  check('C5 no duplicated popover left in editor', !editor.includes('word-count-teleport') && !editor.includes('wc-row'));

  // ── D: wiring assertions (ZenNotePanel workbench notes) ──────────────
  const zen = fs.readFileSync(path.join(__dirname, '..', '..', 'src', 'components', 'ZenNotePanel.vue'), 'utf-8');
  check('D1 zen wires the shared word-count button', zen.includes('WordCountButton') && zen.includes('get-text="wordCountSourceText"'));
  check('D2 zen no longer duplicates the popover', !zen.includes('word-count-teleport') && !zen.includes('wc-row'));
  check('D3 zen spacer pushes button to the far right', zen.includes('toolbar-spacer'));
  check('D4 zen supplies visible text via shared util', zen.includes('visibleEditorText') && zen.includes("from '@/utils/noteWordCount'"));
  check('D5 shared component imports the single counter source', wcBtn.includes("from '@/utils/noteWordCount'"));

  if (failed) {
    console.error(`\n${failed} check(s) FAILED`);
    process.exit(1);
  }
  console.log('\nall checks pass');
}

main().catch((e) => { console.error('ERROR:', e.message); process.exit(1); });
