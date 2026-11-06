/**
 * M6（引文体系改造 2026-10-11）：服务端 citations 载荷 → 前端展示契约。
 * - 卡片=规范 reference 文本（无 URL 的远古籍来源同样成卡）；
 * - 文内 [N] 按样式族（上标/方括号/作者年/注释号）变换为可点击角标；
 * - 代码围栏/既有链接/未知编号不受影响；无载荷时恒等。
 *
 * Run: node --experimental-strip-types tests/workflows/test_citation_payload.cjs
 */
const { buildCitationChips, applyCitationInText } = require('../../src/utils/citations.ts')

const cases = []

const PAYLOAD = {
  style_id: 'gb-t-7714-numeric',
  family: 'numeric-superscript',
  heading: '参考文献',
  entries: [
    { id: 3, num: 1, in_text: '<sup>[1]</sup>', reference: '陈毅. 摄山志:八卷[M]. 刻本. 金陵: 汪志伊, 1790.', url: '', title: '摄山志' },
    { id: 5, num: 2, in_text: '<sup>[2]</sup>', reference: '工信部. 中国人工智能产业发展报告[EB/OL]. (2025-03-01).', url: 'https://example.com/r', title: '中国人工智能产业发展报告' },
  ],
}

// 1. chips: 每被引条目恰一芯片；无 URL 条目同样成卡（原则一）
{
  const chips = buildCitationChips(PAYLOAD)
  cases.push(['chips count = cited entries', chips.length === 2])
  cases.push(['no-url chip keeps reference text', chips[0].bibText.includes('摄山志') && chips[0].bibText.includes('刻本') && chips[0].result.url === ''])
  cases.push(['url chip carries link', chips[1].result.url === 'https://example.com/r'])
}
// 2. 无载荷 → 恒等（旧消息路径不受影响）
{
  const html = '正文 [1]。'
  cases.push(['no payload identity', applyCitationInText(html, null) === html])
  cases.push(['empty entries identity', applyCitationInText(html, { ...PAYLOAD, entries: [] }) === html])
}
// 3. 上标族：注入 class/data-cite-index 保点击
{
  const out = applyCitationInText('正文 [3]。', PAYLOAD)
  cases.push(['superscript family clickable', out.includes('<sup class="citation-ref" data-cite-index="1">[1]</sup>')])
}
// 4. 方括号族（IEEE）：span 包裹
{
  const payload = { ...PAYLOAD, family: 'numeric-bracket', entries: [{ id: 3, num: 1, in_text: '[1]', reference: 'X.', url: '' }] }
  const out = applyCitationInText('先引 [3]。', payload)
  cases.push(['bracket family span', out.includes('<span class="citation-ref" data-cite-index="1">[1]</span>')])
}
// 5. 作者-年族（APA）
{
  const payload = { ...PAYLOAD, family: 'author-date', entries: [{ id: 3, num: 1, in_text: '(陈毅, 1790)', reference: 'X.', url: '' }] }
  const out = applyCitationInText('已有研究 [3]。', payload)
  cases.push(['author-date family span', out.includes('<span class="citation-ref" data-cite-index="1">(陈毅, 1790)</span>')])
}
// 6. 注释族（Chicago notes）：数字渲染为上标
{
  const payload = { ...PAYLOAD, family: 'notes', entries: [{ id: 3, num: 1, in_text: '1', reference: 'X.', url: '' }] }
  const out = applyCitationInText('甲 [3]。', payload)
  cases.push(['notes family sup', out.includes('<sup class="citation-ref" data-cite-index="1">1</sup>')])
}
// 7. 未知编号 / 链接 / 代码围栏不受影响
//   链接用例必须用载荷内存在的编号（[3]）——否则 map miss 先行保护，测不到
//   `(?![(:])` 负前瞻本身（A4.9 测试判别力修复）。
{
  const out = applyCitationInText('未知 [9] 与 [3](https://x.com) 链接。', PAYLOAD)
  cases.push(['unknown marker untouched', out.includes('[9]')])
  cases.push(['markdown link untouched even for mapped id', out.includes('[3](https://x.com)')])
  const fenced = applyCitationInText('<pre><code>[3] code</code></pre> 与 <code>[3]</code> 行内', PAYLOAD)
  cases.push(['code fences untouched', !fenced.includes('citation-ref')])
}
// 8. 数组顺序与 num 排序
{
  const chips = buildCitationChips({ ...PAYLOAD, entries: [...PAYLOAD.entries].reverse() })
  cases.push(['chips sorted by num', chips[0].num === 1 && chips[1].num === 2])
}
// 9. in_text 消毒（A4.9 B2/N1）：转义优先；白名单 <i>/<sup> 还原；未闭合
//    标签（无 `>`，可被外层 </span> 补全执行）也必须被转义。
{
  const payload = { ...PAYLOAD, family: 'author-date', entries: [
    { id: 3, num: 1, in_text: '(Smith, <i>Book</i> <img src=x onerror=alert(1)>)', reference: 'X.', url: '' },
  ] }
  const out = applyCitationInText('引用 [3]。', payload)
  cases.push(['allowed <i> kept', out.includes('<i>Book</i>')])
  cases.push(['hostile tag escaped', !out.includes('<img') && out.includes('&lt;img')])
  const payload2 = { ...PAYLOAD, family: 'author-date', entries: [
    { id: 3, num: 1, in_text: '(Smith <img src=x onerror=window.__xss=1//', reference: 'X.', url: '' },
  ] }
  const out2 = applyCitationInText('引用 [3]。', payload2)
  cases.push(['unterminated hostile tag escaped', !/<img/i.test(out2) && out2.includes('&lt;img')])
}

let failed = 0
for (const [name, ok] of cases) {
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}`)
  if (!ok) failed += 1
}
console.log(`\n=== ${cases.length - failed} pass / ${failed} fail ===`)
process.exit(failed > 0 ? 1 : 0)
