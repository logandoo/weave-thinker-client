/* 清除格式（遗留 #1 方案 A）—— 规则真源的单测（§A4.8 测试先行；RED 先行）。
 *
 * 钉住四件事（纯函数，无 DOM）：
 *  1. 哪些行内标签算「可清除格式」；
 *  2. 哪些元素是「数据载体/语义结构」必须保留（引文角标/尾注/公式/图表/链接/代码）；
 *  3. 哪些 style 属性算字体类脏格式、哪些必须保留（对齐/尺寸）；
 *  4. 范围决策：折叠光标 → 整篇；非折叠 → 选区。
 *
 * 行为级证据由 e2e/note_clear_formatting.spec.ts（8158 生产构建）承担。
 * Run: node --experimental-strip-types tests/workflows/test_clear_formatting.cjs
 */
const path = require('node:path');

async function main() {
  const mod = await import(
    path.resolve(__dirname, '../../src/composables/clearFormatting.ts')
  );
  const {
    isClearableInlineTag,
    isStrippedStyleProperty,
    PROTECTED_SELECTOR,
    PROTECTED_TAGS,
    resolveClearScope,
  } = mod;

  let pass = 0;
  let fail = 0;
  const check = (name, cond, detail) => {
    if (cond) { pass++; console.log(`PASS ${name}`); }
    else { fail++; console.log(`FAIL ${name}${detail === undefined ? '' : ' | ' + JSON.stringify(detail)}`); }
  };

  // ── 1. 可清除的行内标签 ────────────────────────────────────────────────
  for (const tag of ['b', 'strong', 'i', 'em', 'u', 's', 'strike', 'del', 'mark', 'small', 'big', 'font', 'sup', 'sub', 'span']) {
    check(`可清除标签 ${tag}`, isClearableInlineTag(tag) === true);
  }
  for (const tag of ['a', 'pre', 'img', 'table', 'li', 'p', 'blockquote']) {
    check(`保留标签 ${tag}`, isClearableInlineTag(tag) === false);
  }
  check('大小写不敏感', isClearableInlineTag('STRONG') === true);

  // ── 2. 数据载体 / 语义结构 ────────────────────────────────────────────
  for (const cls of ['.citation-ref', '.note-endnote-ref', '.note-endnote-def', '.math-editable', '.mermaid-block', '.echarts-block', '.code-block']) {
    check(`保护选择器含 ${cls}`, PROTECTED_SELECTOR.includes(cls));
  }
  for (const tag of ['A', 'IMG', 'VIDEO', 'AUDIO', 'IFRAME', 'TABLE', 'HR']) {
    check(`保护标签含 ${tag}`, PROTECTED_TAGS.includes(tag));
  }

  // ── 3. 字体类脏格式属性 ──────────────────────────────────────────────
  for (const prop of ['font-family', 'font-size', 'letter-spacing', 'text-transform', 'color', 'background-color', 'line-height', 'font-weight', 'font-style', 'text-decoration', 'text-decoration-line']) {
    check(`剥离属性 ${prop}`, isStrippedStyleProperty(prop) === true);
  }
  for (const prop of ['text-align', 'width', 'height', 'max-width', '--tip-x']) {
    check(`保留属性 ${prop}`, isStrippedStyleProperty(prop) === false);
  }
  check('属性大小写不敏感', isStrippedStyleProperty('FONT-FAMILY') === true);

  // ── 4. 范围决策 ─────────────────────────────────────────────────────
  check('折叠光标 → 整篇', resolveClearScope(true) === 'document');
  check('非折叠 → 选区', resolveClearScope(false) === 'selection');

  // ── 5. 布局标签不剥 style（A4.9 评审：图片宽高/iframe 尺寸是布局） ────────
  for (const tag of ['img', 'video', 'iframe', 'hr', 'math']) {
    check(`布局标签 ${tag}`, mod.isLayoutTag(tag) === true);
  }
  for (const tag of ['td', 'th', 'p', 'span', 'a', 'table']) {
    check(`非布局标签 ${tag}`, mod.isLayoutTag(tag) === false);
  }
  check('解包选择器含 span', mod.CLEARABLE_INLINE_SELECTOR.includes('span'));
  check('行内 code 属可清除格式', mod.CLEARABLE_INLINE_SELECTOR.includes('code'));
  check('行内 code 不再受保护', !mod.PROTECTED_TAGS.includes('CODE'));

  // ── 6. 块级样式归普通段落（2026-10-25 用户更正） ──────────────────────
  for (const tag of ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre']) {
    check(`块级归普通 ${tag}`, mod.isBlockResetTag(tag) === true);
  }
  for (const tag of ['p', 'ul', 'ol', 'li', 'table', 'div']) {
    check(`不归普通 ${tag}`, mod.isBlockResetTag(tag) === false);
  }

  console.log(`RESULT: ${fail === 0 ? 'ALL CHECKS PASS' : fail + ' CHECKS FAILED'} (${pass} pass / ${fail} fail)`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((e) => { console.error('ERROR', e && e.message ? e.message : e); process.exit(1); });
