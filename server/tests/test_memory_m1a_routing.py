"""TDD — W3 M1a 注入层路由（MemAgent 2609.32521 零训练 Begin 门）。

技术规格源：docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §3.1 改进设计（M1a）
+ §10.1 勘正 A1（复用 memory.query_expansion，不做新 purpose）/A2（正名
「注入层选择」——不省 stage1-4 计算，只重组注入顺序）。

- `select_injection_route(query_text, stage0, candidates) -> str` 纯函数：
  确定性门（时间型→episodic-first；偏好/画像型→profile/concept 先行；
  近事→subconscious 先行；默认 full=现状），路由失败/零支撑 fail-open 回全量。
- `_apply_injection_route(...)` 应用层：开关 `memory.provider_routing_enabled`
  （默认关）开启时按路由**重组注入段顺序**（集合不动、预算不动）；关闭时注入
  逐字节不变。
- 开启时 recall log `providers_queried` 追加 `"route:<name>"`。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_m1a_routing.py -v
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_retrieval_service as mrs
from app.services.memory_retrieval_service import RetrievalCandidate, Stage0Result


# ── 测试夹具：多段注入 ctx（仿 _build_injection_context_ex 组装形） ─────────

NOTICE_SUFFIX = (
    "\n<记忆检索状态>[记忆检索] 已检索长期记忆：未找到与该问题直接相关的条目"
    "</记忆检索状态>"
)


def _rich_ctx() -> str:
    blocks = [
        "[用户长期记忆总览]\n- 职业: 后端工程师",
        "[相关事件 Episodic]\n- [2026-10-01] 部署了 weave 服务",
        "[相关概念 Concept]\n- 部署端口（port）: 8158 [权重: 0.80]",
        "[近期 Dream]\n整理了项目记忆",
        "[用户画像 Profile]\n- 职业: 后端工程师",
        "[近期原文片段 Subconscious]\n- [2026-10-13] 刚才说的那个配置 (未经整理)",
        "[澄清提示]\n- 用户澄清\"别再用旧端口\" → 已修正",
        "（部分记忆因长度预算未完整展示）",
    ]
    return "\n\n".join(blocks) + NOTICE_SUFFIX


def _mk_cand(cid: str, tier: str) -> RetrievalCandidate:
    return RetrievalCandidate(id=cid, tier=tier, content="c-" + cid, score=0.5,
                              metadata={"rerank_score": 0.5})


def _cands(*pairs: tuple) -> list:
    return [_mk_cand(cid, tier) for cid, tier in pairs]


def _stage0(query_type: str = "semantic", time_range=None, has_time: bool = False) -> Stage0Result:
    st = Stage0Result(keywords=["kw"], query_type=query_type, time_range=time_range)
    if has_time:
        # has_time = stage0 时间信号（生产由 _stage0_query_expansion 捕获）
        st.has_time = True
    return st


# ── select_injection_route：确定性 + 各门分支 ─────────────────────────────

def test_route_temporal_query_episodic_first():
    cands = _cands(("e1", "episodic"), ("c1", "concept"), ("s1", "subconscious"))
    assert mrs.select_injection_route("昨天说的部署安排", _stage0(), cands) == "temporal_first"


def test_route_temporal_list_query_type():
    cands = _cands(("e1", "episodic"), ("c1", "concept"))
    assert mrs.select_injection_route(
        "列一下", _stage0(query_type="temporal_list"), cands) == "temporal_first"


def test_route_temporal_list_query_type_without_episodic_fails_open():
    cands = _cands(("c1", "concept"))
    assert mrs.select_injection_route(
        "列一下", _stage0(query_type="temporal_list"), cands) == "full"


def test_route_preference_query_profile_concept_first():
    cands = _cands(("c1", "concept"), ("e1", "episodic"), ("s1", "subconscious"))
    assert mrs.select_injection_route("我偏好的代码风格是什么", _stage0(), cands) == "preference_first"


def test_route_recent_query_subconscious_first():
    cands = _cands(("s1", "subconscious"), ("c1", "concept"), ("e1", "episodic"))
    assert mrs.select_injection_route("刚才说的那个配置", _stage0(), cands) == "recent_first"


def test_route_neutral_query_full():
    cands = _cands(("c1", "concept"), ("e1", "episodic"))
    assert mrs.select_injection_route("帮我写一个快排", _stage0(), cands) == "full"


def test_route_empty_query_full():
    assert mrs.select_injection_route("", _stage0(), _cands(("c1", "concept"))) == "full"


def test_route_no_candidates_fails_open_to_full():
    assert mrs.select_injection_route("昨天的安排", _stage0(), []) == "full"
    assert mrs.select_injection_route("我偏好深色主题", _stage0(), []) == "full"


def test_route_deterministic_repeat_calls():
    cands = _cands(("e1", "episodic"), ("c1", "concept"), ("s1", "subconscious"))
    results = {mrs.select_injection_route("上周聊过的书", _stage0(), cands) for _ in range(10)}
    assert len(results) == 1, f"路由选择必须确定性: {results}"


def test_route_independent_of_candidate_order():
    a = _cands(("e1", "episodic"), ("c1", "concept"), ("s1", "subconscious"))
    b = _cands(("s1", "subconscious"), ("c1", "concept"), ("e1", "episodic"))
    q = "我习惯用的编辑器是哪个"
    assert (mrs.select_injection_route(q, _stage0(), a)
            == mrs.select_injection_route(q, _stage0(), b))


# ── _apply_injection_route：应用层（开关关=逐字节不变） ────────────────────

def test_apply_route_disabled_bytes_identical():
    ctx = _rich_ctx()
    out, route = mrs._apply_injection_route(
        "昨天说的部署安排", _stage0(), _cands(("e1", "episodic")), ctx, enabled=False)
    assert out == ctx, "开关关时注入必须逐字节不变"
    assert route == "off"


def test_apply_route_default_reads_config_switch_off(monkeypatch):
    ctx = _rich_ctx()
    monkeypatch.setitem(mrs.config.memory, "provider_routing_enabled", False)
    out, route = mrs._apply_injection_route(
        "昨天说的部署安排", _stage0(), _cands(("e1", "episodic")), ctx)
    assert out == ctx and route == "off"


def test_apply_route_enabled_full_route_bytes_identical():
    ctx = _rich_ctx()
    out, route = mrs._apply_injection_route(
        "帮我写一个快排", _stage0(), _cands(("c1", "concept")), ctx, enabled=True)
    assert route == "full"
    assert out == ctx, "路由 full=现状，注入逐字节不变"


def test_apply_route_enabled_temporal_reorders_sections():
    ctx = _rich_ctx()
    out, route = mrs._apply_injection_route(
        "昨天说的部署安排", _stage0(),
        _cands(("e1", "episodic"), ("c1", "concept")), ctx, enabled=True)
    assert route == "temporal_first"
    blocks = out.split("\n\n")
    assert blocks[0].startswith("[相关事件 Episodic]"), f"episodic 段应先行: {blocks[0][:30]}"
    assert out.endswith(NOTICE_SUFFIX), "尾部缺证据声明必须钉在原尾"
    # 集合不变：仅段序重组
    assert sorted(blocks[:-1]) == sorted(ctx.split("\n\n")[:-1])


def test_apply_route_enabled_preference_puts_profile_first():
    ctx = _rich_ctx()
    out, route = mrs._apply_injection_route(
        "我偏好的代码风格是什么", _stage0(),
        _cands(("c1", "concept"), ("e1", "episodic")), ctx, enabled=True)
    assert route == "preference_first"
    assert out.split("\n\n")[0].startswith("[用户画像 Profile]")
    assert out.endswith(NOTICE_SUFFIX)


def test_apply_route_enabled_recent_puts_subconscious_first():
    ctx = _rich_ctx()
    out, route = mrs._apply_injection_route(
        "刚才说的那个配置", _stage0(),
        _cands(("s1", "subconscious"), ("c1", "concept")), ctx, enabled=True)
    assert route == "recent_first"
    assert out.split("\n\n")[0].startswith("[近期原文片段 Subconscious]")
    assert out.endswith(NOTICE_SUFFIX)


def test_apply_route_enabled_config_switch_on(monkeypatch):
    ctx = _rich_ctx()
    monkeypatch.setitem(mrs.config.memory, "provider_routing_enabled", True)
    out, route = mrs._apply_injection_route(
        "昨天说的部署安排", _stage0(),
        _cands(("e1", "episodic"), ("c1", "concept")), ctx)
    assert route == "temporal_first"
    assert out != ctx
    assert out.endswith(NOTICE_SUFFIX)


def test_apply_route_empty_ctx_safe():
    out, route = mrs._apply_injection_route(
        "昨天", _stage0(), _cands(("e1", "episodic")), "", enabled=True)
    assert out == "" and route == "temporal_first"


# ── 集成缝：_retrieve_with_meta_inner 注入组装后、返回前 ───────────────────

def _patch_stages(monkeypatch, candidates, inject_ctx):
    """把阶段管线桩成固定候选集 + 固定注入 ctx（零 DB/零 LLM）。"""
    async def fake_stage0(query_text, user_id=None, db=None):
        return Stage0Result(keywords=["kw"], query_type="general", include_expired=False)

    async def fake_cold(db, uid):
        return False

    async def fake_stage1(db, uid, stage0, cold_start):
        return list(candidates)

    async def fake_stage2(db, uid, cc, stage0, qt, param_overrides=None):
        return list(cc)

    async def fake_stage3(db, uid, qt, cc, ec, sc, stage0):
        return list(cc) + list(ec) + list(sc)

    async def fake_stage4(cands, qt, user_id=None, db=None, cold_start=False):
        return list(cands)

    async def fake_inject_ex(db, uid, final, stage0, query_text="", **kwargs):
        return (inject_ctx, {"truncated": False, "budget_chars": 2000,
                             "injected_chars": len(inject_ctx)})

    async def fake_boosts(*a, **k):
        return None

    monkeypatch.setattr(mrs, "_stage0_query_expansion", fake_stage0)
    monkeypatch.setattr(mrs, "_is_cold_start", fake_cold)
    monkeypatch.setattr(mrs, "_stage1_bm25_search", fake_stage1)
    monkeypatch.setattr(mrs, "_stage2_description_expansion", fake_stage2)
    monkeypatch.setattr(mrs, "_stage3_embedding_rerank", fake_stage3)
    monkeypatch.setattr(mrs, "_stage4_rerank", fake_stage4)
    monkeypatch.setattr(mrs, "_build_injection_context_ex", fake_inject_ex)
    monkeypatch.setattr(mrs, "_apply_recall_boosts_bg", fake_boosts)
    monkeypatch.setattr(mrs, "_session_cache_get", lambda key: None)
    monkeypatch.setattr(mrs, "_session_cache_put", lambda key, value, meta=None, stats=None: None)
    monkeypatch.setattr(mrs, "_record_window_hit", lambda *a, **k: None)
    monkeypatch.setattr(mrs, "_apply_text_cross_turn_dedup", lambda uid, c: c)
    monkeypatch.setattr(mrs, "_remember_text_injected", lambda uid, ids: None)
    monkeypatch.setattr(mrs, "_emit_recall_log", lambda *a, **k: None)


def _run_retrieve(monkeypatch, query: str, candidates, inject_ctx, capture: dict):
    def fake_emit(user_id, conversation_id, conversation_messages, ctx, ids, top,
                  stats, elapsed_ms):
        capture["ctx"] = ctx
        capture["stats"] = dict(stats)

    _patch_stages(monkeypatch, candidates, inject_ctx)
    monkeypatch.setattr(mrs, "_emit_recall_log", fake_emit)

    async def run():
        return await mrs.retrieve_with_meta(
            None, "u-m1a", [{"role": "user", "content": query}], conversation_id="conv1")

    return asyncio.run(run())


def test_inner_flag_off_injection_bytes_unchanged(monkeypatch):
    baseline = _rich_ctx()
    capture: dict = {}
    monkeypatch.setitem(mrs.config.memory, "provider_routing_enabled", False)
    ctx, _ids, _top = _run_retrieve(
        monkeypatch, "昨天说的部署安排",
        _cands(("e1", "episodic"), ("c1", "concept")), baseline, capture)
    assert ctx == baseline, "开关关时注入必须逐字节不变"
    assert capture["ctx"] == baseline
    assert not any(str(p).startswith("route:") for p in
                   capture["stats"].get("providers_queried") or []), \
        "开关关不得写入 route: 台账标记"


def test_inner_flag_on_reorders_and_logs_route(monkeypatch):
    baseline = _rich_ctx()
    capture: dict = {}
    monkeypatch.setitem(mrs.config.memory, "provider_routing_enabled", True)
    ctx, _ids, _top = _run_retrieve(
        monkeypatch, "昨天说的部署安排",
        _cands(("e1", "episodic"), ("c1", "concept")), baseline, capture)
    assert ctx != baseline, "开关开时注入段序应被重组"
    assert ctx.split("\n\n")[0].startswith("[相关事件 Episodic]")
    assert ctx.endswith(NOTICE_SUFFIX)
    providers = capture["stats"].get("providers_queried") or []
    assert "route:temporal_first" in providers, f"providers_queried 应追加 route: 标记: {providers}"


def test_inner_flag_on_neutral_query_full_route_logged(monkeypatch):
    baseline = _rich_ctx()
    capture: dict = {}
    monkeypatch.setitem(mrs.config.memory, "provider_routing_enabled", True)
    ctx, _ids, _top = _run_retrieve(
        monkeypatch, "帮我写一个快排",
        _cands(("c1", "concept")), baseline, capture)
    assert ctx == baseline, "路由 full=现状，注入逐字节不变"
    providers = capture["stats"].get("providers_queried") or []
    assert "route:full" in providers, f"开启时仍应记 route:<name>: {providers}"


# ── fix-round I4：段切分=节头块（多段落小节不拆散） ─────────────────────────

def _multi_para_ctx() -> str:
    multi = ("[相关事件 Episodic]\n- [2026-10-01] 部署了 weave 服务"
             "\n\n第二段补充：部署后做了压测")
    return "\n\n".join([
        "[用户长期记忆总览]\n- 职业: 后端工程师",
        multi,
        "[相关概念 Concept]\n- 部署端口（port）: 8158 [权重: 0.80]",
    ]) + NOTICE_SUFFIX


def test_apply_route_multiparagraph_section_moves_as_one_block():
    """fix-round I4 判别：段体带空行的小节必须整块换位（节头+后续行直至下一节头
    为一块）。旧实现按 \\n\\n 切段 → 小节被拆散（第二段滞留原槽）。"""
    ctx = _multi_para_ctx()
    out, route = mrs._apply_injection_route(
        "昨天说的部署安排", _stage0(has_time=True),
        _cands(("e1", "episodic"), ("c1", "concept")), ctx, enabled=True)
    assert route == "temporal_first"
    expect = ("[相关事件 Episodic]\n- [2026-10-01] 部署了 weave 服务"
              "\n\n第二段补充：部署后做了压测")
    assert out.startswith(expect), f"多段落小节必须整块换位不得拆散: {out[:120]!r}"
    assert "[用户长期记忆总览]" in out and "[相关概念 Concept]" in out
    assert out.endswith(NOTICE_SUFFIX), "尾部缺证据声明必须钉在原尾"


def test_apply_route_multiparagraph_full_route_bytes_identical():
    """多段落形态下路由 full/off 仍逐字节不变。"""
    ctx = _multi_para_ctx()
    out, route = mrs._apply_injection_route(
        "帮我写一个快排", _stage0(), _cands(("c1", "concept")), ctx, enabled=True)
    assert route == "full" and out == ctx


# ── fix-round I5：时间分支 = stage0.has_time or query_type=="temporal_list" ──

def test_route_stage0_has_time_wiring_production_shape(monkeypatch):
    """fix-round I5：生产形状——_stage0_query_expansion 产出的 stage0 必须携带
    has_time（时间词/LLM 时间信号），时间分支读它（生产可达）。"""
    async def fake_norm(text, user_id=None, db=None):
        return (["k"], None, "semantic", False)  # 生产 4 元组形状（LLM 未给范围）

    monkeypatch.setattr(mrs, "_llm_time_normalization", fake_norm)
    stage0 = asyncio.run(mrs._stage0_query_expansion("昨天说的部署安排", user_id="u", db=None))
    assert stage0.has_time is True, "时间词命中必须在 stage0.has_time 留痕（生产可达）"
    cands = _cands(("e1", "episodic"), ("c1", "concept"))
    assert mrs.select_injection_route("昨天说的部署安排", stage0, cands) == "temporal_first"


def test_route_time_signal_precedes_preference(monkeypatch):
    """fix-round I5 判别：时间信号 ∧ 偏好词同现 → 时间分支先行。
    旧实现仅在 time_range 非空时才走时间分支 → 跌进偏好分支误路由 preference_first。"""
    async def fake_norm(text, user_id=None, db=None):
        return (["k"], None, "semantic", False)

    monkeypatch.setattr(mrs, "_llm_time_normalization", fake_norm)
    q = "上次说过的偏好变更"
    stage0 = asyncio.run(mrs._stage0_query_expansion(q, user_id="u", db=None))
    assert stage0.has_time is True
    cands = _cands(("e1", "episodic"), ("c1", "concept"))
    assert mrs.select_injection_route(q, stage0, cands) == "temporal_first", (
        "时间分支必须改 stage0.has_time or query_type=='temporal_list'（时间信号先行）")


def test_route_has_time_signal_precedes_preference_fixture():
    """fix-round I5 行为判别（独立于接线面）：has_time=True ∧ 偏好词同现 →
    temporal_first。旧实现 time_range=None 时跌进偏好分支 = preference_first。"""
    cands = _cands(("e1", "episodic"), ("c1", "concept"))
    stage0 = _stage0(has_time=True)  # 生产形状：时间词命中、LLM 未给范围
    assert mrs.select_injection_route("上次说过的偏好变更", stage0, cands) == "temporal_first"


def test_route_llm_time_range_signal_temporal_pin(monkeypatch):
    """pin：LLM 时间范围信号（长查询、无时间词）→ has_time → temporal（旧覆盖不回退）。"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    async def fake_norm(text, user_id=None, db=None):
        return (["k"], (now - timedelta(days=7), now), "semantic", False)

    monkeypatch.setattr(mrs, "_llm_time_normalization", fake_norm)
    q = "请把我们讨论过的系统架构演进过程按阶段整理成时间线梳理"  # ≥20 字触发 LLM
    stage0 = asyncio.run(mrs._stage0_query_expansion(q, user_id="u", db=None))
    assert stage0.time_range is not None
    assert stage0.has_time is True
    cands = _cands(("e1", "episodic"), ("c1", "concept"))
    assert mrs.select_injection_route(q, stage0, cands) == "temporal_first"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
