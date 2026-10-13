/* Snippet insert builders (2026-10-03 wave: 笔记/工作台 LaTeX+代码块直接输入).
 * Oracle: pure-function contract of buildCodeBlockHtml + carve-shape check for
 * renderMathSafe (same carve pattern as tests/test_math_tokenizer.cjs).
 * Run: node tests/workflows/test_snippet_insert.cjs */
let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/composables/snippetInsert.ts');
  const { buildCodeBlockHtml, sanitizeCodeLang, encodeMathClosers, escapePuaMarkers, decodeMathClosers, stripMathFraming, mathLiteralHtml } = mod;

  // ── A: basic shape — pre>code with language class, no header chrome ─────
  const html = buildCodeBlockHtml('print("hi")', 'python');
  check('A1 emits pre>code', /<pre><code[^>]*>print\(&quot;hi&quot;\)<\/code><\/pre>/.test(html), html);
  check('A2 language class', html.includes('class="hljs language-python"'), html);
  check('A3 NO code-block-header chrome (serialization leak guard)',
    !html.includes('code-block-header') && !html.includes('code-block-lang') && !html.includes('code-block-copy-btn'), html);
  check('A4 no wrapper .code-block div', !html.includes('class="code-block"'), html);

  // ── B: escaping ─────────────────────────────────────────────────────────
  const esc = buildCodeBlockHtml('<script>alert(1)</script> & "x"', 'js');
  check('B1 code HTML-escaped', esc.includes('&lt;script&gt;') && esc.includes('&amp;') && esc.includes('&quot;x&quot;'), esc);
  check('B2 no raw tag', !esc.includes('<script>'), esc);

  // ── C: language sanitization ────────────────────────────────────────────
  check('C1 hostile lang stripped', buildCodeBlockHtml('x', 'py" onload="x').includes('language-py'), buildCodeBlockHtml('x', 'py" onload="x'));
  check('C2 spaces stripped, + kept (c++)', buildCodeBlockHtml('x', 'c + +').includes('language-c++'), buildCodeBlockHtml('x', 'c + +'));
  const noLang = buildCodeBlockHtml('x', '');
  check('C3 empty lang → bare hljs class', /<code class="hljs">/.test(noLang), noLang);
  const nullLang = buildCodeBlockHtml('x', '   ');
  check('C4 whitespace lang → bare hljs class', /<code class="hljs">/.test(nullLang), nullLang);

  // ── D: multi-line code keeps newlines verbatim ──────────────────────────
  const multi = buildCodeBlockHtml('a\nb\n\tc', 'sh');
  check('D1 newlines verbatim', multi.includes('a\nb\n\tc'), multi);

  // ── E: renderMathSafe carve — math-editable shape (insert path contract) ─
  const fs = await import('fs');
  const path = await import('path');
  const srcPath = path.resolve(__dirname, '../../src/composables/useMarkdown.ts');
  const source = fs.readFileSync(srcPath, 'utf8');
  function sliceFn(name) {
    const re = new RegExp('(?:export\\s+)?function\\s+' + name + '\\s*\\(');
    const m = re.exec(source);
    if (!m) throw new Error('RED: function not found: ' + name);
    const braceStart = source.indexOf('{', m.index);
    let depth = 0;
    for (let j = braceStart; j < source.length; j++) {
      const c = source[j];
      if (c === '{') depth++;
      else if (c === '}') { depth--; if (depth === 0) return source.slice(m.index, j + 1)
        .replace(/^export\s+function/, 'function')
        .replace(/\)\s*:\s*string\s*\{/g, ') {')
        .replace(/:\s*string/g, '')
        .replace(/:\s*boolean/g, ''); }
    }
    throw new Error('unbalanced braces slicing ' + name);
  }
  const katex = require(path.resolve(__dirname, '../../../node_modules/katex'));
  const renderMathSafe = new Function(
    'katex', 'escapeMathPercent',
    sliceFn('escapeMathPercent') + '\n' + sliceFn('renderMathSafe') + '\nreturn renderMathSafe;'
  )(katex, (t) => t.replace(/(?<!\\)%/g, '\\%'));
  const mathHtml = renderMathSafe('x^2+1', false);
  check('E1 math-editable class', mathHtml.includes('class="math-editable"'), mathHtml);
  check('E2 data-tex round-trip attr', mathHtml.includes('data-tex="x^2+1"'), mathHtml);
  check('E3 inline display flag false', mathHtml.includes('data-display-mode="false"'), mathHtml);
  const mathBlock = renderMathSafe('\\frac{1}{2}', true);
  check('E4 display mode true', mathBlock.includes('data-display-mode="true"') && mathBlock.startsWith('<div'), mathBlock);
  check('E5 editable affordance (math-edit-btn)', mathHtml.includes('math-edit-btn'), mathHtml);

  // carve 辅助（与 tests/test_math_tokenizer.cjs 同法：从源码切函数）
  const fs2 = require('fs');
  const path2 = require('path');
  const umSrc = fs2.readFileSync(path2.resolve(__dirname, '../../src/composables/useMarkdown.ts'), 'utf8');
  function sliceFnLocal(name) {
    const re = new RegExp('(?:export\\s+)?function\\s+' + name + '\\s*\\(');
    const m = re.exec(umSrc);
    if (!m) throw new Error('function not found: ' + name);
    const braceStart = umSrc.indexOf('{', m.index);
    let depth = 0;
    for (let j = braceStart; j < umSrc.length; j++) {
      const c = umSrc[j];
      if (c === '{') depth++;
      else if (c === '}') { depth--; if (depth === 0) return umSrc.slice(m.index, j + 1)
        .replace(/^export\s+function/, 'function')
        .replace(/\((\w+):\s*\{[^}]*\}\)/g, '($1)')
        .replace(/\)\s*:\s*[A-Za-z][A-Za-z0-9]*(\s*\|\s*[A-Za-z]+)*\s*\{/g, ') {')
        .replace(/:\s*string/g, '')
        .replace(/:\s*boolean/g, '')
        .replace(/:\s*number/g, ''); }
    }
    throw new Error('unbalanced braces');
  }

  // ── F: sanitizeCodeLang 允许 #（c# / f# 等语言名） ──────────────────────
  check('F1 c# 保留（# 进白名单）', sanitizeCodeLang('c#') === 'c#', sanitizeCodeLang('c#'));
  check('F2 f# 保留', sanitizeCodeLang('f#') === 'f#', sanitizeCodeLang('f#'));
  check('F3 引号/空格仍剥', sanitizeCodeLang('py" x') === 'pyx', sanitizeCodeLang('py" x'));

  // ── G: buildCodeFence —— 真 marked 往返（RED：未实现则报错） ────────────
  const { buildCodeFence } = mod;
  const marked = require(path.resolve(__dirname, '../../../node_modules/marked'));
  function fenceCode(md) {
    const tokens = marked.lexer(md);
    const t = tokens.find((tk) => tk.type === 'code');
    return t ? t.text : null;
  }
  const blanky = '\n\nfoo\n\n';
  const g1 = buildCodeFence('sh', blanky);
  check('G1 emit 为围栏块', g1.includes('```sh') && g1.includes('foo'), g1);
  const g1r = fenceCode(g1);
  check('G2 首尾空行 round-trip 保真（真 marked）', g1r === blanky, JSON.stringify(g1r));
  const inner = '```\ninner fence\n```';
  const g3 = buildCodeFence('md', inner);
  check('G3 内嵌三反引号 → 四反引号围栏', g3.includes('\n````md\n'), g3.slice(0, 20));
  const g3r = fenceCode(g3);
  check('G4 内嵌围栏 round-trip 保真', g3r === inner, JSON.stringify(g3r));
  const g5 = buildCodeFence('python', 'print(1)');
  check('G5 普通代码仍三反引号', g5.includes('\n```python\n'), g5);
  const g5r = fenceCode(g5);
  check('G6 普通代码 round-trip', g5r === 'print(1)', JSON.stringify(g5r));

  // ── E6: katex 抛错也保 math-editable + data-tex（错误契约；假 katex 强制走 catch） ──
  const throwingKatex = { renderToString() { throw new Error('boom') } };
  const renderMathSafeThrow = new Function(
    'katex', 'escapeMathPercent',
    sliceFnLocal('escapeMathPercent') + '\n' + sliceFnLocal('renderMathSafe') + '\nreturn renderMathSafe;'
  )(throwingKatex, (t) => t);
  const errHtml = renderMathSafeThrow('x^2', true);
  check('E6 katex 抛错仍保 math-editable + data-tex', errHtml.includes('math-editable') && errHtml.includes('data-tex="x^2"'), errHtml);

  // ── H: serializeMathDelimited —— tex 含 $ 用 \(...\)/\[...\] 定界（carve） ──
  const serMd = new Function('encodeMathClosers', 'escapePuaMarkers', 'isRealDisplayMath',
    sliceFnLocal('serializeMathDelimited') + '\nreturn serializeMathDelimited;'
  )(encodeMathClosers, escapePuaMarkers, new Function(sliceFnLocal('isRealDisplayMath') + '\nreturn isRealDisplayMath;')());
  check('H1 普通行内仍 $…$', serMd('x^2', false) === '$x^2$', serMd('x^2', false));
  check('H2 含 $ 行内 → \\(…\\)', serMd('a$b', false) === '\\(a$b\\)', serMd('a$b', false));
  check('H3 含 $ 块级 → \\[…\\]', serMd('a$$b', true, true) === '\\[a$$b\\]', serMd('a$$b', true, true));
  check('H4 普通块级仍 $$…$$（compact）', serMd('x', true, true) === '$$x$$', serMd('x', true, true));

  // ── I: collapseExtraBlankLines —— 围栏感知（RED：未实现） ──────────────
  const { collapseExtraBlankLines } = mod;
  const fenced = '\n\`\`\`sh\n\n\nfoo\n\n\n\`\`\`\n\n\n\nprose\n\n\n\n\nend\n';
  const ci = collapseExtraBlankLines(fenced);
  check('I1 围栏内 3+ 连行不折叠', ci.includes('sh\n\n\nfoo\n\n\n\`\`\`'), ci);
  check('I2 围栏外 3+ 连行折叠为 2', ci.includes('prose\n\nend'), ci);
  check('I3 无围栏文本仍折叠', collapseExtraBlankLines('a\n\n\n\n\nb') === 'a\n\nb', collapseExtraBlankLines('a\n\n\n\n\nb'));
  const four = '\n\`\`\`\`md\n\n\`\`\`\nx\n\`\`\`\n\n\`\`\`\`\n\n\n\nprose\n';
  check('I4 四反引号围栏同样受保护', collapseExtraBlankLines(four).includes('\n\`\`\`\nx\n\`\`\`\n\n\`\`\`\`'), collapseExtraBlankLines(four));

  // ── J: buildCodeFence 敌意 lang/文本边角 ─────────────────────────────
  const hostile = buildCodeFence('py" onload="x', 'x');
  check('J1 敌意 lang 进不了属性（无引号游离）', !hostile.includes('onload="x'), hostile);
  const edge = buildCodeFence('md', '\`\`\`\ntrailing-run');
  check('J2 文本以反引号游程开头也加长围栏', edge.includes('\n\`\`\`\`md\n'), edge.slice(0, 12));
  const allTicks = buildCodeFence('txt', '\`\`\`\`\`');
  check('J3 纯反引号文本 round-trip', fenceCode(allTicks) === '\`\`\`\`\`', JSON.stringify(fenceCode(allTicks)));

  // ── M/N: tokenizer 全链往返（carve createMathExtension + 真 marked）──
  const um = umSrc;
  function sliceTs(name) {
    const re2 = new RegExp('(?:export\\s+)?function\\s+' + name + '\\s*\\(');
    const m2 = re2.exec(um);
    if (!m2) throw new Error('RED: function not found: ' + name);
    const bs = um.indexOf('{', m2.index);
    let d = 0;
    for (let j = bs; j < um.length; j++) {
      if (um[j] === '{') d++;
      else if (um[j] === '}') { d--; if (d === 0) return um.slice(m2.index, j + 1)
        .replace(/^export\s+function/, 'function')
        .replace(/\((\w+):\s*\{[^}]*\}\)/g, '($1)')
        .replace(/\)\s*:\s*[A-Za-z][A-Za-z0-9]*(\s*\|\s*[A-Za-z]+)*\s*\{/g, ') {')
        .replace(/:\s*string/g, '').replace(/:\s*boolean/g, '').replace(/:\s*number/g, '')
        .replace(/:\s*any/g, ''); }
    }
    throw new Error('unbalanced');
  }
  const markedMod = marked;
  const katexReal = require(path.resolve(__dirname, '../../../node_modules/katex'));
  const LATEX_MARKER_RE = /[\\^_{}]/;
  const renderReal = new Function('katex', 'escapeMathPercent',
    sliceTs('escapeMathPercent') + '\n' + sliceTs('renderMathSafe') + '\nreturn renderMathSafe;'
  )(katexReal, (t) => t.replace(/(?<!\\)%/g, '\\%'));
  const isReal = new Function(sliceTs('isRealDisplayMath') + '\nreturn isRealDisplayMath;')();
  const isMathish = new Function('LATEX_MARKER_RE', sliceTs('isMathishInner') + '\nreturn isMathishInner;')(LATEX_MARKER_RE);
  const createExt = new Function('renderMathSafe', 'isRealDisplayMath', 'isMathishInner', 'LATEX_MARKER_RE', 'escapeMathPercent', 'decodeMathClosers', 'stripMathFraming',
    sliceTs('escapeMathPercent') + '\n' + sliceTs('isMathishInner') + '\n' + sliceTs('isRealDisplayMath') + '\n' + sliceTs('createMathExtension') + '\nreturn createMathExtension;'
  )(renderReal, isReal, isMathish, LATEX_MARKER_RE, (t) => t.replace(/(?<!\\)%/g, '\\%'), decodeMathClosers, stripMathFraming);
  const markedInst = new markedMod.Marked();
  markedInst.use({ breaks: true, gfm: true });
  markedInst.use(createExt());
  function mathTex(md) {
    // inline 级 tokenizer 只在 render/inline 阶段跑——走 parse 后从 data-tex 提取
    const html = markedInst.parse(md);
    const all = [...html.matchAll(/data-tex="([^"]*)"/g)].map((x) => x[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&'));
    return all.length ? all.join('\u0000FRAG\u0000') : null;
  }

  // M 族：框架换行精确剥离（不再 trim 吃用户边缘空白）
  check('M1 块级 emit 的边缘空格 round-trip', mathTex(serMd(' x ', true)) === ' x ', JSON.stringify(mathTex(serMd(' x ', true))));
  check('M2 括号 emit 的边缘空格 round-trip', mathTex(serMd(' x ', false)) === ' x ', JSON.stringify(mathTex(serMd(' x ', false))));
  check('M3 手写 \[ x \] 边缘空格保留', mathTex('\\[ x \\]') === ' x ', JSON.stringify(mathTex('\\[ x \\]')));
  check('M4 普通块级不受影响', mathTex(serMd('x^2', true)) === 'x^2', JSON.stringify(mathTex(serMd('x^2', true))));

  // N 族：tex 含闭符序列不早闭（serializeMathDelimited 编码 + tokenizer 解码）
  check('N1 行内 \) 早闭已修（全链）', mathTex(serMd('a\\)b$c', false)) === 'a\\)b$c', JSON.stringify(mathTex(serMd('a\\)b$c', false))));
  check('N2 块级 \] 早闭已修（全链）', mathTex(serMd('a\\]b$c', true)) === 'a\\]b$c', JSON.stringify(mathTex(serMd('a\\]b$c', true))));
  check('N3 无碰撞 tex 零编码（源可读）', serMd('f(x) = $5', false) === '\\(f(x) = $5\\)', serMd('f(x) = $5', false));
  check('N4 普通行内不受影响', mathTex(serMd('x^2', false)) === 'x^2', JSON.stringify(mathTex(serMd('x^2', false))));

  // ── P 族：本轮评审实锤的三个销毁/泄漏路径（全链） ────────────────────
  // P1: 块级 tex 带边缘换行 + $ —— 旧括号发射被空行劈段=二次保存公式消失
  check('P1 边缘换行块级 tex 全链保真', mathTex(serMd('\nx$y', true)) === '\nx$y', JSON.stringify(mathTex(serMd('\nx$y', true))));
  // P2: 行内多行 tex —— $…$ 家族 [^$\n] 拒收，必须走括号形态
  check('P2 行内多行 tex 全链保真', mathTex(serMd('a\nb', false)) === 'a\nb', JSON.stringify(mathTex(serMd('a\nb', false))));
  // P3: 输入本身含 PUA —— 编解码恒等（此前 decode 无条件毁原文）
  const puaTex = 'a\uE000b\\)c';
  check('P3 PUA 输入编解码恒等', decodeMathClosers(encodeMathClosers(puaTex, false)) === puaTex,
    JSON.stringify(decodeMathClosers(encodeMathClosers(puaTex, false))));
  // P4: display tex 含 $$ —— 走括号形态仍全链
  check('P4 含 $$ 块级 tex 全链保真', mathTex(serMd('a$$b', true)) === 'a$$b', JSON.stringify(mathTex(serMd('a$$b', true))));
  // P5: \r 边缘（CRLF 源）不落 $…$ 家族
  check('P5 \r 缘 tex 走括号形态', serMd('a\rb', false) !== '$a\rb$', serMd('a\rb', false));

  // ── Q 族：round4fix 复审实锤（发射矩阵回旋镖 + 转义层开环） ──────────────
  // Q1: 输入字面转义码自身（E005-8 类）恒等（旧转义层开环）
  check('Q1 转义码字面输入恒等', decodeMathClosers(encodeMathClosers('a\uE005b', false)) === 'a\uE005b',
    JSON.stringify(decodeMathClosers(encodeMathClosers('a\uE005b', false))));
  // Q2: 输入字面标记符（E0FF）恒等（闭合转义）
  check('Q2 标记符字面输入恒等', decodeMathClosers(encodeMathClosers('a\uE0FF0b', false)) === 'a\uE0FF0b',
    JSON.stringify(decodeMathClosers(encodeMathClosers('a\uE0FF0b', false))));
  // Q3: money 形态块级（99 = $5）全链——块形态被 money-guard 拒=二次保存毁（新 Critical）
  check('Q3 money 形态块级全链保真', mathTex(serMd('99 = $5', true)) === '99 = $5', JSON.stringify(mathTex(serMd('99 = $5', true))));
  // Q4: 块级含单 $（x$y）——导出 [^$] 正则排除 $，必须走方括号形态
  check('Q4 含单 \$ 块级发射走方括号', serMd('x$y', true).includes('\\['), serMd('x$y', true));
  // Q5: CR 系（\r\rb 空行劈段 / a\rb 漂移）
  check('Q5a CR-CR 块内不劈段', mathTex(serMd('a\r\rb', false)) === 'a\r\rb', JSON.stringify(mathTex(serMd('a\r\rb', false))));
  check('Q5b CR 缘无 LF 漂移', mathTex(serMd('a\rb', false)) === 'a\rb', JSON.stringify(mathTex(serMd('a\rb', false))));

  // Q5c: display 路径 CR（money+CR 二次保存毁的实锤用例）
  check('Q5c 块级 CR 形态全链保真（money+CR）', mathTex(serMd('99 元\rx^2', true)) === '99 元\rx^2', JSON.stringify(mathTex(serMd('99 元\rx^2', true))));
  check('Q5d 块级 CR-CR 全链保真', mathTex(serMd('a\r\rb', true)) === 'a\r\rb', JSON.stringify(mathTex(serMd('a\r\rb', true))));

  // ── R 族：遗留收尾四钉 ─────────────────────────────────────────────
  // R1: USD 99 类过 gate=分类器特性（非销毁）——钉住其往返完好=verified-safe
  check('R1 USD 99 块级分类特性往返完好', mathTex(serMd('USD 99', true)) === 'USD 99', JSON.stringify(mathTex(serMd('USD 99', true))));
  check('R2 99 USD 同', mathTex(serMd('99 USD', true)) === '99 USD', JSON.stringify(mathTex(serMd('99 USD', true))));
  // R3: compact 文本抽取路径——字面提示用实体美元（前端与导出双侧不解析=一致）
  const lit = mathLiteralHtml('x^2', false);
  check('R3 字面提示实体美元（双侧不解析）', lit.includes('&#36;x^2&#36;') && !lit.includes('$x^2$'), lit);
  const litD = mathLiteralHtml('a<b', true);
  check('R4 块级字面 + HTML 转义（< 不得成标签）', litD.includes('&#36;&#36;a&lt;b&#36;&#36;') && !litD.includes('<b'), litD);
  check('R5 字面内容含定界符也双侧安全', !mathLiteralHtml('a\\)b', false).includes('\\)'), mathLiteralHtml('a\\)b', false));

  console.log(failed === 0 ? 'RESULT: all pass' : `RESULT: ${failed} failed`);
  process.exit(failed === 0 ? 0 : 1);
}
main().catch((e) => { console.error(e); process.exit(1); });
