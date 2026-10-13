"""TDD — 记忆 P1-① 采纳反馈闭环（被引用→权重/边权回写）+ P1-② 一致性加权 + P2 策略路由。

纯逻辑核心（匹配/加权/路由）hermetic 单测；PG 写路径由
tests/workflows/memory_implicit_recall_probe.py（真库探针）覆盖。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_adoption_feedback.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── P1-① match_adopted_concepts（纯函数） ────────────────────────────────

class TestMatchAdoptedConcepts:
    def test_name_match(self):
        from app.services.memory_adoption_service import match_adopted_concepts
        concepts = [{"id": "c1", "canonical_name": "Titanic 偏好", "aliases": "[]"}]
        assert match_adopted_concepts(concepts, "我觉得 Titanic 偏好这块说得对") == ["c1"]

    def test_alias_match_case_insensitive(self):
        from app.services.memory_adoption_service import match_adopted_concepts
        concepts = [{"id": "c1", "canonical_name": "浪漫爱情片", "aliases": '["romance", "爱情片"]'}]
        got = match_adopted_concepts(concepts, "a Romance movie night, 爱情片 也行")
        assert got == ["c1"]

    def test_no_match_and_too_short_alias_ignored(self):
        from app.services.memory_adoption_service import match_adopted_concepts
        concepts = [
            {"id": "c1", "canonical_name": "泰坦尼克号", "aliases": '["a", "bb"]'},
            {"id": "c2", "canonical_name": "科幻", "aliases": "[]"},
        ]
        assert match_adopted_concepts(concepts, "今晚吃火锅 bb 不去") == []

    def test_malformed_aliases_json_fails_open(self):
        from app.services.memory_adoption_service import match_adopted_concepts
        concepts = [{"id": "c1", "canonical_name": "科幻", "aliases": "{broken"}]
        assert match_adopted_concepts(concepts, "科幻好看") == ["c1"]


# ── P1-② apply_cross_modal_consistency（纯函数，只对验证过证据） ─────────

def _cand(cid, lex, dense, score=0.5):
    return {"id": cid, "score": score,
            "metadata": {"lex_rank": lex, "dense_rank": dense}}


class TestCrossModalConsistency:
    def test_agreement_bonus_only_for_verified(self):
        from app.services.memory_retrieval_service import apply_cross_modal_consistency
        a, b = _cand("v1", 0, 1), _cand("u1", 0, 1)
        apply_cross_modal_consistency([a, b], verified_ids={"v1"}, bonus=0.05, damp=0.03)
        assert abs(a["score"] - 0.55) < 1e-9   # 验证过+两模态一致 → 加分
        assert abs(b["score"] - 0.5) < 1e-9    # 未验证 → 零改动

    def test_disagreement_damp(self):
        from app.services.memory_retrieval_service import apply_cross_modal_consistency
        a = _cand("v1", 0, 9)
        apply_cross_modal_consistency([a], verified_ids={"v1"}, bonus=0.05, damp=0.03)
        assert abs(a["score"] - 0.47) < 1e-9   # 一前一后 → 减分

    def test_empty_verified_noop_and_missing_ranks_noop(self):
        from app.services.memory_retrieval_service import apply_cross_modal_consistency
        a, b = _cand("x", 0, 0), {"id": "y", "score": 0.5, "metadata": {}}
        apply_cross_modal_consistency([a, b], verified_ids=set())
        assert a["score"] == 0.5 and b["score"] == 0.5

    def test_delta_survives_composite_via_calibrated(self):
        """A4.9 wave2 Critical 回归钉：增量必须落 calibrated_score（composite
        以它重算终分，只动 score 会被覆盖=死代码）。"""
        from app.services.memory_retrieval_service import (
            apply_cross_modal_consistency, _composite_score_by_tier)
        st = type("S", (), {"keywords": [], "query_type": "semantic", "include_expired": False})()
        a = type("C", (), {})()
        a.id, a.tier, a.score = "v1", "concept", 0.5
        a.metadata = {"lex_rank": 0, "dense_rank": 1, "calibrated_score": 0.6,
                      "weight": 0.5, "importance": 0.5, "activation_strength": 1.0}
        apply_cross_modal_consistency([a], verified_ids={"v1"}, bonus=0.05, damp=0.03)
        assert abs(a.metadata["calibrated_score"] - 0.65) < 1e-9
        out = _composite_score_by_tier([a], st)
        base = type("C", (), {})()
        base.id, base.tier, base.score = "v1", "concept", 0.5
        base.metadata = {"calibrated_score": 0.6, "weight": 0.5,
                         "importance": 0.5, "activation_strength": 1.0}
        out_base = _composite_score_by_tier([base], st)
        assert out[0].score > out_base[0].score, "一致性增量必须体现在 composite 终分"


# ── P2 select_retrieval_profile（纯函数） ─────────────────────────────────

class TestStrategyRouting:
    def test_entity_dense_by_keyword_density(self):
        from app.services.memory_retrieval_service import select_retrieval_profile
        st = type("S", (), {"keywords": ["titanic", "浪漫", "电影"], "query_type": "semantic"})()
        assert select_retrieval_profile(
            "titanic 浪漫 电影", st, {"strategy_route_enabled": True}) == "entity_dense"

    def test_narrative_when_sparse_keywords(self):
        from app.services.memory_retrieval_service import select_retrieval_profile
        st = type("S", (), {"keywords": ["电影"], "query_type": "semantic"})()
        assert select_retrieval_profile(
            "我周末有点无聊想找点事情做放松一下", st, {"strategy_route_enabled": True}) == "narrative"

    def test_route_disabled_returns_default(self):
        from app.services.memory_retrieval_service import select_retrieval_profile
        st = type("S", (), {"keywords": ["titanic", "浪漫", "电影"], "query_type": "semantic"})()
        cfg = {"strategy_route_enabled": False}
        assert select_retrieval_profile("titanic 浪漫 电影", st, cfg) == "default"

    def test_default_is_disabled(self):
        # D-q8：默认关（house style）——空 cfg 零行为变化
        from app.services.memory_retrieval_service import select_retrieval_profile
        st = type("S", (), {"keywords": ["titanic", "浪漫", "电影"], "query_type": "semantic"})()
        assert select_retrieval_profile("titanic 浪漫 电影", st, {}) == "default"

    def test_profile_params_differ_and_cfg_overrides(self):
        from app.services.memory_retrieval_service import (
            STRATEGY_PROFILE_DEFAULTS, strategy_profile_params)
        e = strategy_profile_params("entity_dense", {})
        n = strategy_profile_params("narrative", {})
        assert e["stage2_relation_max_new"] > n["stage2_relation_max_new"]
        assert e["stage2_relation_score_decay"] > n["stage2_relation_score_decay"]
        # rho/AGPR 维持 opt-in（两档默认均不开，不改热路径既有行为）
        assert "expansion_rho_enabled" not in e and "expansion_rho_enabled" not in n
        over = strategy_profile_params(
            "entity_dense", {"strategy": {"entity_dense": {"stage2_relation_max_new": 3}}})
        assert over["stage2_relation_max_new"] == 3
        assert over["stage2_relation_score_decay"] == STRATEGY_PROFILE_DEFAULTS[
            "entity_dense"]["stage2_relation_score_decay"]
