// 项3（UPSTREAM_TODO_20261004）：abort/重连追平判定的分页感知回归腿。
//
// 背景：caught-up 启发式 expectedMessageCount=内存长度+1 与分页瘦身刷新
// （MESSAGE_PAGE_SIZE=50 窗口）不相容——≥49 条历史消息的会话上窗口化刷新后
// 列表长度被夹在 50，length>=expected 永不满足 → Phase-1 快路径失灵 →
// fall-through 叠 local-abort-* 重复气泡 + endStreaming 延迟。
//
// 修复：锚定判定 caughtUpAfterAnchor（锚=本轮发起前最后一条服务器消息 id，
// need=本轮期望新增持久化消息数）；锚被挤出窗口 → null（调用方降级全量拉取再判）。
//
// Run: node --experimental-strip-types tests/workflows/test_abort_caughtup_paged.cjs

const { caughtUpAfterAnchor, isSyntheticMessageId, recoveryCaughtUp, trimWindowAfterFullFetch } = require('../../src/stores/streamReducer.ts')

let failures = 0
function check(name, cond) {
  if (cond) { console.log(`PASS ${name}`) } else { failures += 1; console.error(`FAIL ${name}`) }
}

const U = (id) => ({ id, role: 'user' })
const A = (id) => ({ id, role: 'assistant' })

// ── 场景 1：≥50 条历史 + 窗口化刷新后判追平（本波核心）──────────────────
// 50 窗：[m1..m48, U', A']；锚=m48（发送前最新服务器消息）；need=2
{
  const history = []
  for (let i = 1; i <= 48; i++) history.push(i % 2 ? U(`m${i}`) : A(`m${i}`))
  const windowAfterCommit = [...history, U('u-new'), A('a-new')]  // 恰好 50
  const r = caughtUpAfterAnchor(windowAfterCommit, { id: 'm48', need: 2 })
  check('S1 ≥50历史窗口内锚后 2 条持久化（U+A）→ 追平', r === true)
}

// ── 场景 2：同窗但未落库（只有用户消息持久化，助手未存）→ 未追平 ─────────
{
  const history = []
  for (let i = 1; i <= 48; i++) history.push(i % 2 ? U(`m${i}`) : A(`m${i}`))
  const list = [...history, U('u-new')]
  check('S2 锚后仅 U（助手未落库）→ false', caughtUpAfterAnchor(list, { id: 'm48', need: 2 }) === false)
}

// ── 场景 3：合成气泡（乐观用户气泡 temp-）不计入持久化 ──────────────────
{
  const history = []
  for (let i = 1; i <= 48; i++) history.push(i % 2 ? U(`m${i}`) : A(`m${i}`))
  // 服务端尚未 commit：窗口尾部只挂着合成 temp- 气泡
  const preCommit = [...history, U('temp-1700000000000')]
  check('S3 锚后仅合成气泡 → false', caughtUpAfterAnchor(preCommit, { id: 'm48', need: 2 }) === false)
  // commit 后合成气泡被保留在尾（preserveSynthetic 窗口语义）——持久化判定须跳过它
  const postCommitWithSynth = [...history, U('u-new'), A('a-new'), U('temp-1700000000000')]
  check('S3b commit 后尾部残留合成气泡仍判追平（跳过合成）', caughtUpAfterAnchor(postCommitWithSynth, { id: 'm48', need: 2 }) === true)
}

// ── 场景 4：最新持久化是 user（异常形态：助手行缺失）→ 未追平 ────────────
{
  const list = [A('m1'), U('u1'), U('u2')]
  check('S4 尾部持久化为 user → false', caughtUpAfterAnchor(list, { id: 'm1', need: 2 }) === false)
}

// ── 场景 5：锚被挤出窗口 → null（不可判定，调用方降级）──────────────────
{
  const list = []
  for (let i = 60; i <= 110; i++) list.push(i % 2 ? U(`m${i}`) : A(`m${i}`))
  check('S5 锚不在窗内 → null', caughtUpAfterAnchor(list, { id: 'm1', need: 2 }) === null)
}

// ── 场景 6：空会话首 turn（anchor.id=null）──────────────────────────────
{
  check('S6 空会话 U+A 落库 → true', caughtUpAfterAnchor([U('u1'), A('a1')], { id: null, need: 2 }) === true)
  check('S6b 空会话仅 U 落库 → false', caughtUpAfterAnchor([U('u1')], { id: null, need: 2 }) === false)
  check('S6c 空会话全空 → false', caughtUpAfterAnchor([], { id: null, need: 2 }) === false)
}

// ── 场景 7：stop 语义 need=1（cancel 落定部分回答）──────────────────────
{
  const list = [U('u1'), A('a-partial')]
  check('S7 stop：锚后部分回答已存 → true', caughtUpAfterAnchor(list, { id: 'u1', need: 1 }) === true)
  const notYet = [U('u1')]
  check('S7b stop：部分回答未存 → false', caughtUpAfterAnchor(notYet, { id: 'u1', need: 1 }) === false)
}

// ── 场景 8：合成 id 族全覆盖（双谓词强断言——谓词漂移即红，不用 || 弱化）──
{
  // isSyntheticMessageId 覆盖族（streamReducer 既有谓词）
  const synthCore = ['local-abort-1', 'bg-1', 'resume-1', 'resume-complete-1', 'temp-1']
  for (const id of synthCore) {
    check(`S8a ${id} isSyntheticMessageId`, isSyntheticMessageId(id) === true)
  }
  // 追平过滤覆盖族（含插话族——caughtUpAfterAnchor 的 NON_PERSISTED_ID）
  const synthAll = [...synthCore, 'interject-pending-1', 'interject-committed-1']
  for (const id of synthAll) {
    const r = caughtUpAfterAnchor([U('u0'), { id, role: 'assistant' }], { id: 'u0', need: 1 })
    check(`S8b ${id} 追平不计`, r === false)
  }
}


// ── W3-项1 r2（2026-10-06）：恢复尾部形态判定（假阳性+假阴性双向）──
{
  // 原假阳性：上一轮 [u1,a1] + 本轮合成用户气泡——旧 user 锚回退 u1，a1 满足
  // need=1+assistant-tail → 误判收线。
  check('W3-1a 本轮未落库（合成在尾）→ false（原假阳性消除）',
    recoveryCaughtUp([U('u1'), A('a1'), U('temp-99')]) === false)
  // r1 假阴性：答案已落库且已入列表——any-role 锚=答案本身 → after=[] → 硬 false。
  check('W3-1b 答案已落库 → true（假阴性消除）',
    recoveryCaughtUp([U('u1'), A('a1'), U('u2'), A('a2')]) === true)
  check('W3-1c U\'已落库、答案未到 → false',
    recoveryCaughtUp([U('u1'), A('a1'), U('u2')]) === false)
  check('W3-1d 无合成行历史形态 → true（W3 前旧语义保留）',
    recoveryCaughtUp([U('u1'), A('a1')]) === true)
  check('W3-1e 空列表 → false', recoveryCaughtUp([]) === false)
  check('W3-1f 全合成 → false', recoveryCaughtUp([U('temp-1'), A('local-abort-2')]) === false)
  check('W3-1g 合成行在持久化行之前不影响 → true',
    recoveryCaughtUp([U('temp-old'), U('u1'), A('a1')]) === true)
}

// ── W3-项3（2026-10-06）：收窗保锚（仅 verdict=true 收窗）─────────────
{
  const long = Array.from({ length: 60 }, (_, i) => ({ id: `m${i}` }))
  check('W3-3a verdict=false 不收窗（保锚可判）', trimWindowAfterFullFetch(long, false, 50).length === 60)
  check('W3-3b verdict=true 收窗', trimWindowAfterFullFetch(long, true, 50).length === 50)
  check('W3-3c 短列表不动', trimWindowAfterFullFetch([{ id: 'a' }], true, 50).length === 1)
}

console.log(failures === 0 ? '\nRESULT: all passed' : `\nRESULT: ${failures} failed`)
process.exit(failures === 0 ? 0 : 1)
