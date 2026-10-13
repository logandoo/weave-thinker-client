/* 标题字号泄漏（需求 3）—— Enter→Backspace 后焦点右侧字号放大。
 *
 * 根因（Chromium）：标题内 insertParagraph 切块 + deleteContentBackward 合块时，
 * Blink 的 HandleStyleSpansBeforeInsertion / FollowBlockElementStyle 会把合并点
 * 右侧内容包进 `<span style="font-size:...">`（WPT 用例名即
 * merge-span-with-style-after-pressing-enter-followed-by-backspace-in-contenteditable-div）。
 * 该 span 的 em 值落在标题内会与标题自身 em 相乘 → 右侧文字放大。
 *
 * 本测试钉住纯逻辑：何时归一化（inputType 集合）+ 归一化哪些块 + 剥离动作本身。
 * 行为级 10 次重复实测由 Playwright 承担（tests/verification_log.md）。
 *
 * Run: node tests/workflows/test_heading_typography.cjs */
const path = require('path');

let failed = 0;
let passed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1; else passed += 1;
}

// ── 极简假 DOM（鸭子类型，满足 TypoElement 契约）────────────────────────────
function el(tag, styleProps = {}, children = []) {
  const node = {
    nodeType: 1,
    tagName: String(tag).toUpperCase(),
    style: {
      _props: { ...styleProps },
      removeProperty(p) { delete this._props[p]; },
    },
    _attrs: {},
    parentNode: null,
    childNodes: children,
    get firstChild() { return this.childNodes[0] || null; },
    getAttribute(n) {
      if (n === 'style') {
        const ks = Object.keys(this.style._props);
        return ks.length ? ks.map((k) => `${k}:${this.style._props[k]}`).join(';') : '';
      }
      return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
    },
    setAttribute(n, v) { this._attrs[n] = v; },
    removeAttribute(n) { delete this._attrs[n]; },
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => {
        for (const c of n.childNodes) {
          if (c.nodeType !== 1) continue;
          if (sel === '*' || c.tagName === String(sel).toUpperCase()) out.push(c);
          walk(c);
        }
      };
      walk(node);
      return out;
    },
    insertBefore(child, ref) {
      const old = child.parentNode;
      if (old && old !== node) {
        const j = old.childNodes.indexOf(child);
        if (j >= 0) old.childNodes.splice(j, 1);
      }
      const i = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
      this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, child);
      child.parentNode = this;
      return child;
    },
    removeChild(c) {
      const i = this.childNodes.indexOf(c);
      if (i >= 0) this.childNodes.splice(i, 1);
      c.parentNode = null;
      return c;
    },
    normalize() {},
  };
  children.forEach((c) => { c.parentNode = node; });
  return node;
}
function text(s) { return { nodeType: 3, textContent: s, parentNode: null }; }
function dump(n) {
  if (n.nodeType === 3) return n.textContent;
  const style = Object.keys(n.style._props).length
    ? ` style="${Object.entries(n.style._props).map(([k, v]) => `${k}:${v}`).join(';')}"`
    : '';
  return `<${n.tagName.toLowerCase()}${style}>${n.childNodes.map(dump).join('')}</${n.tagName.toLowerCase()}>`;
}

async function main() {
  let mod = null;
  try {
    mod = await import(path.resolve(__dirname, '..', '..', 'src', 'composables', 'headingTypography.ts'));
  } catch (e) {
    console.log('FAIL module import (missing/unimplemented): ' + (e && e.message));
    failed += 1;
    console.log(`\nRESULT: 0 passed, ${failed} failed`);
    process.exit(1);
  }
  const {
    shouldNormalizeHeadingAfterInput, isHeadingTag,
    normalizeHeadingBlocksAround, HEADING_TYPO_PROPS,
  } = mod;

  // ── G1 触发集合 ────────────────────────────────────────────────────────
  check('G1a insertParagraph 触发', shouldNormalizeHeadingAfterInput('insertParagraph') === true);
  check('G1b deleteContentBackward 触发', shouldNormalizeHeadingAfterInput('deleteContentBackward') === true);
  check('G1c deleteContentForward 触发', shouldNormalizeHeadingAfterInput('deleteContentForward') === true);
  check('G1d insertLineBreak 触发', shouldNormalizeHeadingAfterInput('insertLineBreak') === true);
  check('G1e insertText 不触发（保护用户自设字号）', shouldNormalizeHeadingAfterInput('insertText') === false);
  check('G1f undefined 不触发', shouldNormalizeHeadingAfterInput(undefined) === false);
  check('G1g insertFromPaste 不触发', shouldNormalizeHeadingAfterInput('insertFromPaste') === false);

  // ── G2 标题判定 ────────────────────────────────────────────────────────
  check('G2a h1..h6 是标题', ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'].every(isHeadingTag));
  check('G2b p 不是标题', isHeadingTag('p') === false);
  check('G2c 大小写不敏感', isHeadingTag('H2') === true);

  // ── G3 泄漏剥离（核心：Chromium 合块伪影）────────────────────────────────
  const leakedSpan = el('span', { 'font-size': '1.3em' }, [text('纵览')]);
  const heading = el('h2', {}, [text('世界'), leakedSpan]);
  const root = el('div', {}, [heading]);
  const n = normalizeHeadingBlocksAround(heading, null);
  check('G3a 归一化了 1 个标题块', n === 1, { n });
  check('G3b span 的 font-size 被剥离', leakedSpan.style._props['font-size'] === undefined, leakedSpan.style._props);
  check('G3c 文本逐字保留', dump(heading).includes('世界') && dump(heading).includes('纵览'), dump(heading));
  check('G3d 无放大字号残留', !dump(heading).includes('font-size'), dump(heading));

  // ── G4 排版五属性全剥，颜色/背景保留 ────────────────────────────────────
  const span2 = el('span', {
    'font-size': '33px', 'font-family': 'serif', 'font-weight': '700',
    'line-height': '2', 'letter-spacing': '2px', color: 'red', 'background-color': 'yellow',
  }, [text('X')]);
  const h2 = el('h1', { 'font-size': '22px', color: 'blue' }, [span2]);
  normalizeHeadingBlocksAround(h2, null);
  const left = Object.keys(span2.style._props);
  check('G4a 五个排版属性全被剥离', HEADING_TYPO_PROPS.every((p) => !left.includes(p)), left);
  check('G4b color/background 保留', left.includes('color') && left.includes('background-color'), left);
  check('G4c 标题自身排版属性剥离', h2.style._props['font-size'] === undefined && h2.style._props.color === 'blue', h2.style._props);

  // ── G5 <font> 展开保文本（既有语义不得回归）─────────────────────────────
  const font = el('font', {}, [text('字号')]);
  font._attrs.size = '6';
  const h3 = el('h3', {}, [text('前'), font, text('后')]);
  normalizeHeadingBlocksAround(h3, null);
  check('G5a <font> 被展开', h3.querySelectorAll('font').length === 0, dump(h3));
  check('G5b 文本保留', dump(h3).includes('字号'), dump(h3));

  // ── G6 非标题块不动（正文里的用户字号是合法设置）────────────────────────
  const userSpan = el('span', { 'font-size': '30px' }, [text('大字')]);
  const para = el('p', {}, [userSpan]);
  const n6 = normalizeHeadingBlocksAround(para, null);
  check('G6a 正文块不归一化', n6 === 0, { n6 });
  check('G6b 正文内字号保留', userSpan.style._props['font-size'] === '30px', userSpan.style._props);

  // ── G7 Backspace 合块：前一兄弟是标题也要归一化 ─────────────────────────
  const prevHeading = el('h2', {}, [text('世界')]);
  const mergedSpan = el('span', { 'font-size': '1.3em' }, [text('纵览')]);
  prevHeading.childNodes.push(mergedSpan);
  mergedSpan.parentNode = prevHeading;
  const nextPara = el('p', {}, [text('下一')]);
  const n7 = normalizeHeadingBlocksAround(nextPara, prevHeading);
  check('G7a 前一兄弟标题被归一化', n7 === 1, { n7 });
  check('G7b 合入的 span 字号被剥离', mergedSpan.style._props['font-size'] === undefined, mergedSpan.style._props);

  // ── G8 空 style 属性一并清掉（避免 style="" 垃圾）───────────────────────
  const span3 = el('span', { 'font-size': '1em' }, [text('Y')]);
  const h4 = el('h4', {}, [span3]);
  normalizeHeadingBlocksAround(h4, null);
  check('G8a 空 style 属性被清空/移除', !span3.getAttribute('style'), { style: span3.getAttribute('style') });

  // ── G9 接线：标记不得跨事件存活（评审复审 New-1）──────────────────────
  {
    const fs = require('fs');
    const path2 = require('path');
    const src = fs.readFileSync(path2.resolve(__dirname, '..', '..', 'src', 'components', 'WysiwygEditor.vue'), 'utf8');
    const onInput = src.slice(src.indexOf('function onInput('), src.indexOf('function onInput(') + 2200);
    const onKeydown = src.slice(src.indexOf('function onKeydown('), src.indexOf('function onKeydown(') + 4000);
    check('G9a onInput 对任何 native input 都消费掉标记',
      /if \(nativeEvent\) \{[\s\S]*pendingHeadingNormalize = false/.test(onInput));
    check('G9b onKeydown 对非触发键清掉陈旧标记',
      /e\.key !== 'Enter' && e\.key !== 'Backspace' && e\.key !== 'Delete'/.test(onKeydown));
    check('G9c 归一化需要 inputType 与标记双门槛',
      /shouldNormalizeHeadingAfterInput\(nativeEvent\.inputType\) && pendingHeadingNormalize/.test(onInput));
  }

  console.log(`\nRESULT: ${passed} passed, ${failed} failed`);
  if (failed) {
    console.log('RESULT: FAILED');
    process.exit(1);
  }
  console.log('RESULT: ALL CHECKS PASS');
}

main().catch((e) => { console.error(e); process.exit(1); });
