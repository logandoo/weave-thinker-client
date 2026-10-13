/* Sidebar conversation ordering tests (2026-09-15, wave 2: deferred minors).
 *
 * Covers:
 *   - comparator mirrors backend ORDER BY (sort_order ASC null→0, recency DESC,
 *     both-NULL activity → first = PG DESC NULLS FIRST)          [A4.9 Minor-1]
 *   - promote reconcile is CLOCK-SKEW IMMUNE: caught-up is decided by
 *     comparing SERVER timestamps against each other, never Date.now()
 *     against a server timestamp                                 [A4.9 Minor-2]
 *   - rollback restores the pre-promote fields when a send is rejected
 *     without being delivered                                     [A4.9 Minor-3]
 *   - stale snapshot re-applies the local promote / caught-up clears it
 *     (A4.9 Important-1 regression guard)
 * Run: node tests/workflows/test_conversation_order.cjs */
let failed = 0;
function check(name, cond, detail) {
  console.log(`${cond ? 'PASS' : 'FAIL'} ${name}${detail !== undefined && !cond ? ' | ' + JSON.stringify(detail) : ''}`);
  if (!cond) failed += 1;
}

async function main() {
  const mod = await import('../../src/stores/conversationOrder.ts');
  const {
    conversationActivityMs,
    serverLastUserMessageMs,
    serverCaughtUp,
    compareConversationsBySidebarOrder,
    reconcilePendingPromotions,
    rollbackPendingPromotion,
  } = mod;

  const iso = (ms) => new Date(ms).toISOString();
  const T0 = Date.parse('2026-09-15T06:00:00.000Z');
  const conv = (id, sort_order, last_user_message_at, updated_at) => ({
    id, sort_order, last_user_message_at, updated_at,
  });
  const pendingRec = (over = {}) => ({
    promotedAt: Date.now(),
    previousActivityMs: 0,
    previousSortOrder: 0,
    previousLastUserMessageAt: null,
    ...over,
  });

  // ── A: comparator mirrors backend ORDER BY (sort_order ASC, recency DESC) ──
  const a = conv('a', 0, iso(T0), iso(T0));
  const b = conv('b', 1, iso(T0 + 60_000), iso(T0 + 60_000));
  const c = conv('c', 0, iso(T0 + 120_000), iso(T0 + 120_000));
  check('A1 sort_order dominates recency', compareConversationsBySidebarOrder(a, b) < 0);
  check('A2 same band → newer recency first', compareConversationsBySidebarOrder(c, a) < 0);
  const sorted = [a, b, c].sort(compareConversationsBySidebarOrder).map((x) => x.id);
  check('A3 full order = [c, a, b]', JSON.stringify(sorted) === JSON.stringify(['c', 'a', 'b']), sorted);

  // ── B: NULL / missing fields behave like the backend contract ──────────────
  const nullSort = conv('n', null, iso(T0 + 300_000), iso(T0 + 300_000));
  const undefSort = conv('u', undefined, iso(T0 + 300_000), iso(T0 + 300_000));
  const zeroTwin = conv('z', 0, iso(T0 + 300_000), iso(T0 + 300_000));
  check('B1 null sort_order ≡ 0 (same band, same recency → tie)',
    compareConversationsBySidebarOrder(nullSort, zeroTwin) === 0);
  check('B2 undefined sort_order ≡ 0', compareConversationsBySidebarOrder(undefSort, nullSort) === 0);
  const fallback = conv('f', 0, null, iso(T0 + 600_000));
  check('B3 no user msg → updated_at fallback', conversationActivityMs(fallback) === T0 + 600_000);
  const broken = conv('x', 0, 'not-a-date', 'also-bad');
  check('B4 unparseable timestamp → 0 (last), no NaN poisoning',
    conversationActivityMs(broken) === 0 && compareConversationsBySidebarOrder(broken, a) > 0);
  // A4.9 Minor-1: PG `ORDER BY coalesce(luma, updated_at) DESC` puts both-NULL
  // FIRST (DESC default NULLS FIRST) — the comparator must match.
  const bothNull = conv('bn', 0, null, null);
  const bnSorted = [a, bothNull, c].sort(compareConversationsBySidebarOrder).map((x) => x.id);
  check('B5 both-NULL activity → first (PG DESC NULLS FIRST)',
    conversationActivityMs(bothNull) === Number.POSITIVE_INFINITY && bnSorted[0] === 'bn', bnSorted);

  // ── C: pending promotion survives a STALE snapshot (Important-1 race) ──────
  const pending = {};
  const promotedAt = T0 + 900_000;
  pending['t'] = pendingRec({ promotedAt, previousActivityMs: T0 + 120_000 });
  const stale = [
    conv('sib0', 0, iso(T0), iso(T0)),
    conv('sib1', 1, iso(T0 + 60_000), iso(T0 + 60_000)),
    conv('t', 2, iso(T0 + 120_000), iso(T0 + 120_000)),
  ];
  const changed = reconcilePendingPromotions(stale, pending);
  const staleOrder = stale.map((x) => x.id);
  check('C1 stale snapshot re-applies the local promote (target first)',
    changed === true && staleOrder[0] === 't', staleOrder);
  check('C2 pending marker survives while the server has not caught up',
    pending['t'] !== undefined, Object.keys(pending));
  check('C3 no collateral: siblings keep their relative order',
    JSON.stringify(staleOrder.filter((id) => id !== 't')) === JSON.stringify(['sib0', 'sib1']), staleOrder);

  // ── D: caught-up snapshot clears the marker and applies server values ──────
  const caughtUp = [
    conv('t', 0, iso(T0 + 120_001), iso(T0 + 120_001)),
    conv('sib0', 0, iso(T0), iso(T0)),
  ];
  const changed2 = reconcilePendingPromotions(caughtUp, pending);
  check('D1 caught-up snapshot leaves server order (already target-first)',
    changed2 === false && caughtUp.map((x) => x.id)[0] === 't', caughtUp.map((x) => x.id));
  check('D2 pending marker cleared once the server caught up', pending['t'] === undefined, pending);

  // ── E: no pending marker → reconcile is a no-op ────────────────────────────
  const untouched = [conv('p', 5, iso(T0), iso(T0)), conv('q', 0, iso(T0 + 1000), iso(T0 + 1000))];
  const before = JSON.stringify(untouched);
  check('E1 no pending → no mutation', reconcilePendingPromotions(untouched, {}) === false &&
    JSON.stringify(untouched) === before);

  // ── F: clock-skew immunity (A4.9 Minor-2) ──────────────────────────────────
  // Server clock far behind the client's Date.now() must NOT look "caught up".
  const skewed = {};
  skewed['t'] = pendingRec({
    promotedAt: Date.now() + 365 * 24 * 3600 * 1000, // client clock ~1 year ahead
    previousActivityMs: T0,
    previousLastUserMessageAt: iso(T0),
    previousSortOrder: 2,
  });
  const staleSkew = [conv('t', 2, iso(T0), iso(T0)), conv('sib', 0, iso(T0 + 10_000), iso(T0 + 10_000))];
  reconcilePendingPromotions(staleSkew, skewed);
  check('F1 clock skew (client ahead) → stale snapshot still re-applies the promote',
    staleSkew[0].id === 't' && skewed['t'] !== undefined, staleSkew.map((x) => x.id));
  check('F2 serverCaughtUp is decided by server timestamps only',
    serverCaughtUp(conv('t', 0, iso(T0), iso(T0)), skewed['t']) === false &&
    serverCaughtUp(conv('t', 0, iso(T0 + 1), iso(T0 + 1)), skewed['t']) === true);
  check('F3 serverLastUserMessageMs ignores updated_at fallback',
    serverLastUserMessageMs(conv('t', 0, null, iso(T0 + 999))) === 0 &&
    serverLastUserMessageMs(conv('t', 0, iso(T0 + 5), iso(T0))) === T0 + 5);
  // F4: client clock BEHIND — the OLD `serverMs >= promotedAt` check falsely
  // concluded "caught up" and cleared the marker; the server-domain comparison
  // must keep the pending promote (marker kept + sort_order re-applied).
  // NOTE: the optimistic last_user_message_at itself is display-only and may
  // rank low under a behind clock — the server value arrives via refresh.
  const behind = {};
  behind['t'] = pendingRec({
    promotedAt: Date.now() - 365 * 24 * 3600 * 1000, // client clock ~1 year behind
    previousActivityMs: T0,
    previousLastUserMessageAt: iso(T0),
    previousSortOrder: 2,
  });
  const staleBehind = [conv('t', 2, iso(T0), iso(T0)), conv('sib', 0, iso(T0 + 10_000), iso(T0 + 10_000))];
  reconcilePendingPromotions(staleBehind, behind);
  check('F4 clock skew (client behind) → marker kept + promote re-applied (no false catch-up)',
    behind['t'] !== undefined && staleBehind.find((c) => c.id === 't').sort_order === 0,
    staleBehind.map((x) => x.id));

  // ── G: rollback restores the pre-promote fields (A4.9 Minor-3) ─────────────
  const p2 = {};
  const target = conv('r', 4, iso(T0 + 50_000), iso(T0 + 50_000));
  p2['r'] = pendingRec({
    promotedAt: T0 + 900_000,
    previousActivityMs: T0 + 50_000,
    previousLastUserMessageAt: iso(T0 + 50_000),
    previousSortOrder: 4,
  });
  // simulate the optimistic promote
  target.sort_order = 0;
  target.last_user_message_at = iso(T0 + 900_000);
  const rolled = rollbackPendingPromotion(target, p2);
  check('G1 rollback restores sort_order / last_user_message_at and clears the marker',
    rolled === true && target.sort_order === 4 && target.last_user_message_at === iso(T0 + 50_000) &&
    p2['r'] === undefined, target);
  check('G2 rollback with no marker → no-op',
    rollbackPendingPromotion(conv('nope', 1, null, null), {}) === false);

  // ── H: regenerate promotion semantics (2026-09-26) ─────────────────────────
  // 后端活动时间口径 = MAX(全部消息 created_at)：regenerate 只新增助手消息、
  // 不产生用户消息，但服务端返回的 last_user_message_at 字段仍随新助手消息
  // 推进 → 乐观置顶的追平判定（serverCaughtUp）照常生效，不会被旧快照打回。
  const hPending = {
    previousActivityMs: T0 + 60_000, previousSortOrder: 2,
    previousLastUserMessageAt: iso(T0 + 60_000), promotedAt: Date.now(),
  };
  check('H1 regenerate: assistant-newer server activity counts as caught up',
    serverCaughtUp(conv('h', 0, iso(T0 + 900_000), iso(T0 + 900_000)), hPending) === true);
  check('H2 regenerate: pre-completion snapshot (assistant not yet persisted) does NOT catch up',
    serverCaughtUp(conv('h', 0, iso(T0 + 60_000), iso(T0 + 60_000)), hPending) === false);

  check('H3 comparator: 活动时间更晚者居上（regenerate 语义下同口径排序键）',
    compareConversationsBySidebarOrder(
      conv('rg', 0, iso(T0 + 900_000), iso(T0)),
      conv('old', 0, iso(T0 + 60_000), iso(T0 + 60_000)),
    ) < 0);

  console.log(failed === 0 ? '\nall checks pass' : `\n${failed} check(s) FAILED`);
  process.exit(failed === 0 ? 0 : 1);
}

main();
