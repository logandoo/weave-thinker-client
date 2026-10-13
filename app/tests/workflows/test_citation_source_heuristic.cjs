/**
 * Citation 来源-heuristic (W14, prod conv f1499710, 2026-10-09): the inline
 * 来源 patterns were designed for image-search annotations "来源：XXX [N]"
 * but fired on ANY prose "X来源：" — "4. 技术来源：尊界车上…超20家[30]"
 * produced ONE bogus bibliography entry, maxCitationIndex collapsed to 1,
 * and only [1] of 31 cited markers rendered as a badge (chips likewise).
 * The strip regex additionally ATE the prose sentence ("来源：…[30]" → "[30]").
 * Contract: compound "X来源：" (CJK char immediately before 来源) is prose,
 * never an annotation; bare/标点/加粗 "来源：" annotations still parse/strip.
 *
 * Run: node --experimental-strip-types tests/workflows/test_citation_source_heuristic.cjs
 */
const { parseBibliographyEntries, INLINE_SOURCE_REGEX, maxCitationIndexFor, assembleChips } = require('../../src/utils/citations.ts')

const cases = []

const PROSE = '4. 技术来源：尊界车上搭载的乾崑智驾、鸿蒙座舱等来自引望体系（原车BU资产），尊界是引望的客户之一，其客户名单已覆盖鸿蒙智行"五界"及外部品牌超20家[30][36]。'

// 1. prose 技术来源： must NOT produce a bogus bibliography entry
{
  const entries = parseBibliographyEntries(PROSE)
  cases.push(['prose 技术来源 yields no bib entry', entries.length === 0])
}
// 2. prose with real cited markers around it stays clean end-to-end
{
  const content = '组织上是这样切分的[11][14]。\n\n' + PROSE + '\n\n后续内容[2]。'
  const entries = parseBibliographyEntries(content)
  cases.push(['mixed prose still no bib entry', entries.length === 0])
}
// 3. genuine line-initial annotation still parses
{
  const entries = parseBibliographyEntries('配图如下。\n来源：观察者网 [3]\n\n正文继续[1]')
  cases.push(['line-initial 来源 annotation parses', entries.length === 1 && entries[0].num === 3 && entries[0].text.includes('观察者网')])
}
// 4. bold annotation parses
{
  const entries = parseBibliographyEntries('**来源：** 界面新闻 [2]')
  cases.push(['bold 来源 annotation parses', entries.length === 1 && entries[0].num === 2])
}
// 5. annotation after sentence-final punctuation parses
{
  const entries = parseBibliographyEntries('上图所示。来源：IT之家 [5]')
  cases.push(['post-punctuation 来源 annotation parses', entries.length === 1 && entries[0].num === 5])
}
// 6. strip must NOT eat the prose sentence (the 技术来源 clause survives intact)
{
  const stripped = PROSE.replace(INLINE_SOURCE_REGEX, '[$1]')
  cases.push(['prose sentence survives strip', stripped === PROSE])
}
// 7. genuine annotation still collapses to [N]
{
  const stripped = '配图。\n来源：Pexels [1]\n正文'.replace(INLINE_SOURCE_REGEX, '[$1]')
  cases.push(['genuine annotation collapses to [N]', stripped.includes('[1]') && !stripped.includes('Pexels')])
}
// 8. compound words in other scripts/prefixes are prose too (review R2 范围修复)
{
  const cases8 = [
    '信息来源：新华社 [3]的报道更完整。',
    'GPT来源：OpenAI博客 [1]有说明。',
    '㐗来源：某论文 [2]提出了方法。',
    'データの来源：報告書 [4]によると。',
  ]
  cases.push(['compound 信息来源/GPT来源/Ext-A/kana yields no bib entry',
    cases8.every((c) => parseBibliographyEntries(c).length === 0)])
}
// 9. 3-digit markers strip/parse consistently (digit-width unify \d{1,3})
{
  const stripped = '来源：新华网 [100]'.replace(INLINE_SOURCE_REGEX, '[$1]')
  const entries = parseBibliographyEntries('来源：新华网 [100]')
  cases.push(['3-digit marker parses AND strips', stripped === '[100]' && entries.length === 1 && entries[0].num === 100])
}
// 10. maxCitationIndexFor (input = MATCHED bib entries only — fabricated
//     unmatched entries are filtered by the caller and must never inflate the
//     range): a short matched bib must NOT collapse the range
{
  const oneBib = [{ num: 3, text: '观察者网' }]
  const sparseBib = [{ num: 1, text: 'a' }, { num: 5, text: 'b' }]
  cases.push(['short matched bib does not collapse range',
    maxCitationIndexFor(oneBib, 37) === 37
    && maxCitationIndexFor(sparseBib, 3) === 5
    && maxCitationIndexFor([], 37) === 37
    && maxCitationIndexFor([], 0) === 0])
}
// 11. assembleChips: one chip PER referenced marker — bib interpretation wins,
//     positional fallback covers the rest (1-entry bib + refs {1..3} → 3 chips
//     with nums 1/2/3, not 1 chip; click-resolvability > URL dedupe)
{
  const results = [
    { title: 'r1', url: 'https://a.example/1' },
    { title: 'r2', url: 'https://a.example/2' },
    { title: 'r3', url: 'https://a.example/3' },
  ]
  const matched = [{ entry: { num: 3, text: '观察者网' }, result: results[0] }]
  const refs = new Set([1, 2, 3])
  const numMap = new Map([[1, 1], [2, 2], [3, 3]])
  const chips = assembleChips(matched, results, refs, numMap)
  const nums = chips.map((c) => c.num)
  cases.push(['one chip per referenced marker (bib wins, positional fills)',
    chips.length === 3 && nums.includes(1) && nums.includes(2) && nums.includes(3)
    && chips.find((c) => c.num === 3)?.result.url === 'https://a.example/1'])
}
// 12. prose with an ASCII-letter-prefix compound survives strip intact
{
  const prose = 'GPT来源：OpenAI博客 [1]写得很清楚。'
  cases.push(['letter-prefix prose survives strip', prose.replace(INLINE_SOURCE_REGEX, '[$1]') === prose])
}

let fails = 0
for (const [name, ok] of cases) {
  if (!ok) fails += 1
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`)
}
console.log(fails === 0 ? '\nall checks pass' : `\n${fails} FAILURES`)
process.exit(fails === 0 ? 0 : 1)
