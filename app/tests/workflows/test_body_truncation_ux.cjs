/**
 * 正文截断 UX 根治（实施波 A2/B2，2026-10-03）——纯函数契约测试
 * Run: cd frontend && node --experimental-strip-types tests/workflows/test_body_truncation_ux.cjs
 *
 * 红线：__truncated__ 的 text 段必须换回全文（content_segments → message.content 逐级兜底）；
 * 工具/思考预览不动；正文 <8000 字永不折叠。
 */
const { resolveDisplaySequence, forceFullBody } = require('../../src/stores/streamReducer.ts')

const cases = []
const t = (name, fn) => cases.push([name, fn])

const BODY = '正' + '文'.repeat(5840) // 5841 字

t('A2-1 __truncated__ text 段换回 content_segments 全文', () => {
  const items = [
    { type: 'reasoning_step', content: '思'.repeat(200), __truncated__: true, __size_bytes__: 65704 },
    { type: 'tool', content: '{"a":1}', __truncated__: true },
    { type: 'text', content: BODY.slice(0, 500), __truncated__: true, __size_bytes__: 5841 },
  ]
  const out = resolveDisplaySequence(items, [BODY], BODY)
  if (out[2].content !== BODY) throw new Error(`正文仍为 ${out[2].content.length} 字`)
  if (out[2].__truncated__) throw new Error('换回全文后应清除 __truncated__ 标记')
})

t('A2-2 无 content_segments 时回落 message.content', () => {
  const items = [{ type: 'text', content: BODY.slice(0, 500), __truncated__: true }]
  const out = resolveDisplaySequence(items, [], BODY)
  if (out[0].content !== BODY) throw new Error('回落失败')
})

t('A2-3 多 text 段按序映射 content_segments', () => {
  const s1 = '一'.repeat(2000)
  const s2 = '二'.repeat(3000)
  const items = [
    { type: 'text', content: s1.slice(0, 500), __truncated__: true },
    { type: 'tool', content: 'x' },
    { type: 'text', content: s2.slice(0, 500), __truncated__: true },
  ]
  const out = resolveDisplaySequence(items, [s1, s2], s1 + s2)
  if (out[0].content !== s1 || out[2].content !== s2) throw new Error('多段映射错位')
})

t('A2-4 工具/思考预览保持原样（性能不回吐）', () => {
  const items = [
    { type: 'reasoning_step', content: '思'.repeat(500), __truncated__: true, __size_bytes__: 65704 },
    { type: 'tool', content: 'x'.repeat(500), __truncated__: true },
    { type: 'text', content: BODY.slice(0, 500), __truncated__: true },
  ]
  const out = resolveDisplaySequence(items, [BODY], BODY)
  if (out[0].content.length !== 500 || out[1].content.length !== 500) throw new Error('非正文段被误改')
  if (!out[0].__truncated__) throw new Error('工具/思考的截断标记须保留（徽标数据源）')
})

t('A2-5 未截断 text 段不动、无标记时零开销', () => {
  const items = [{ type: 'text', content: '完整短回答。' }]
  const out = resolveDisplaySequence(items, ['别的'], '完整短回答。')
  if (out[0].content !== '完整短回答。') throw new Error('未截断段被误改')
})

t('A2-6 校验和不符（错段风险）→ 不换、保标记', () => {
  const seg = '一'.repeat(2000) // 比如压缩路径清空后错位塞进来的段
  const items = [{ type: 'text', content: '正'.repeat(500), __truncated__: true, __size_bytes__: 5841 }]
  const out = resolveDisplaySequence(items, [seg], '正'.repeat(5841))
  if (out[0].content.length !== 500) throw new Error('错段被当全文渲染')
  if (!out[0].__truncated__) throw new Error('错段情形必须保留截断标记（徽标数据源）')
})

t('A2-7 多 text 段无 segments → 不换、保标记（降级诚实）', () => {
  const items = [
    { type: 'text', content: '一'.repeat(500), __truncated__: true },
    { type: 'text', content: '二'.repeat(500), __truncated__: true },
  ]
  const out = resolveDisplaySequence(items, [], '一二'.repeat(500))
  if (out[0].__truncated__ !== true || out[1].__truncated__ !== true) throw new Error('不可拆分拼接体不得换文')
})

t('A2-8 含 emoji 正文（UTF-16 口径）仍按校验和愈合', () => {
  const full = '正'.repeat(5840) + '\u{1F600}' // 5841 码位 = 5842 UTF-16 码元
  const want = full.length // 后端 __size_bytes__=_item_text_total：UTF-16 码元（W6 定稿）
  const items = [{ type: 'text', content: full.slice(0, 500), __truncated__: true, __size_bytes__: want }]
  const out = resolveDisplaySequence(items, [full], full)
  if (!out[0].content || out[0].content.length === 500) throw new Error('emoji 正文未愈合（UTF-16 语义失配）')
  if (out[0].__truncated__) throw new Error('愈合后应清标记')
})

t('A2-9 forceFullBody：未愈合多段并为一条 message.content 全量（#8 终极兜底）', () => {
  const body = '末条回答全文。'.repeat(10)
  const items = [
    { type: 'tool', content: 'x' },
    { type: 'text', content: '一'.repeat(500), __truncated__: true },
    { type: 'text', content: '二'.repeat(500), __truncated__: true },
  ]
  const out = forceFullBody(items, body)
  const texts = out.filter((i) => i.type === 'text')
  if (texts.length !== 1) throw new Error('未愈合段应合并为一条')
  if (texts[0].content !== body) throw new Error('合并段必须是 message.content 全量')
  if (out[0].type !== 'tool') throw new Error('工具项须原位保留')
})

t('A2-10 W6 后端真实形态（UTF-16 计数）必须愈合；错值仍拒绝', () => {
  const full = '文'.repeat(3000) + '😀'.repeat(3) // UTF-16: 3006（3000+3×2）；码点: 3003
  const want = full.length // = _item_text_total（后端签发值）
  const mk = (w) => [{ type: 'text', content: full.slice(0, 500), __truncated__: true, __size_bytes__: w }]
  const good = resolveDisplaySequence(mk(want), [full], full)
  if (good[0].content !== full || good[0].__truncated__) {
    throw new Error('UTF-16 口径校验和必须愈合（含 astral 字符）')
  }
  const bad = resolveDisplaySequence(mk(want - 1), [full], full)
  if (bad[0].__truncated__ !== true || bad[0].content.length !== 500) {
    throw new Error('错值必须保标记不换文（诚实降级）')
  }
})

// B2 折叠阈值测试已移除（2026-10-05 用户指令：正式回答绝对不可截断、折叠——
// bodyClampable/BODY_CLAMP_THRESHOLD 随实现一并删除；正文保真由上面的
// 愈合/兜底用例承担）。

let failed = 0
for (const [name, fn] of cases) {
  try { fn(); console.log(`PASS ${name}`) } catch (e) { failed += 1; console.log(`FAIL ${name} | ${e.message}`) }
}
console.log(`RESULT: ${cases.length - failed} passed, ${failed} failed`)
process.exit(failed ? 1 : 0)
