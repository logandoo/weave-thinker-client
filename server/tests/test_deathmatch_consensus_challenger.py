"""TDD — W1 D2a 完成前共识挑战者（论文 2610.00972 VeriHarness 采纳件；勘正 C2）
（docs/AUDIT_2026-10-13_deathmatch_memory_papers.md §2.2/§10）。

judge 判 done 且 reconcile 之前调用一次 fresh-context LLM：对照验收标准 ×
settled 台账 × 工作区证据，输出①无直接证据支撑的验收标准 ②一条被共享但未
受挑战的主张；结果作为 reconcile 附加证据/issue 注入（fail-open，无新硬闸）。
开关 deathmatch_consensus_challenger_enabled 默认关（A/B）。
C2 去重契约：与占位判定分工（覆盖面 vs 执行面），缓存键=验收标准哈希+
证据快照哈希+轮次，互不重复计费。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_deathmatch_consensus_challenger.py -v
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


def _conv(**over):
    base = dict(
        id="conv-d2a",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标",
        deathmatch_turns=3,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": [
            {"id": "s1", "status": "done", "description": "步骤1", "expected_output": "a.md"},
        ]},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_grilling_completed=0,
        deathmatch_grilling_total=0,
        deathmatch_grilling_round=0,
        deathmatch_grilling_round_total=3,
        deathmatch_human_gate=None,
        deathmatch_bible_draft=None,
        deathmatch_settled_ledger=[{"type": "step_complete", "summary": "步骤1 完成"}],
        deathmatch_subgoals=["验收标准A：产出 a.md"],
        deathmatch_acceptance_criteria=None,
        deathmatch_failed_directions=None,
        deathmatch_pause_state=None,
        deathmatch_events=None,
        deathmatch_no_progress_replans=0,
        deathmatch_context_summary=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _switch():
    dm = ds.config._config.setdefault("deathmatch", {})
    prev = dm.get("consensus_challenger_enabled")
    dm["consensus_challenger_enabled"] = True
    ds._CONSENSUS_CACHE.clear()
    yield
    if prev is None:
        dm.pop("consensus_challenger_enabled", None)
    else:
        dm["consensus_challenger_enabled"] = prev
    ds._CONSENSUS_CACHE.clear()


def test_disabled_by_default_returns_none():
    dm = ds.config._config.setdefault("deathmatch", {})
    dm["consensus_challenger_enabled"] = False
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def boom(*a, **k):
        raise AssertionError("switch off 时不得调用 LLM")

    mgr._llm_generate = boom
    assert _run(mgr._challenge_completion_consensus("回复", "/tmp/ws-none")) is None


def test_reports_unsupported_criteria_and_claim(tmp_path):
    ws = str(tmp_path)
    (tmp_path / "a.md").write_text("内容 " * 80, encoding="utf-8")
    conv = _conv()
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return json.dumps({
            "unsupported_criteria": ["验收标准A：产出 a.md"],
            "unchallenged_claim": "声称已导出 PDF",
            "claim_verdict": "无直接证据",
        }, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    out = _run(mgr._challenge_completion_consensus("已完成", ws))
    assert out and out.get("unsupported_criteria"), "须报出无证据支撑的验收标准"
    assert out.get("unchallenged_claim")
    assert "验收标准" in captured["prompt"], "验收标准须进入挑战者输入"
    _events = [e for e in (conv.deathmatch_events or []) if e.get("type") == "consensus_challenge"]
    assert _events, "须记 consensus_challenge 事件"


def test_fail_open_on_llm_error(tmp_path):
    conv = _conv()
    mgr = DeathmatchManager(conv)

    async def boom(*a, **k):
        raise RuntimeError("llm down")

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = boom
    assert _run(mgr._challenge_completion_consensus("r", str(tmp_path))) is None


def test_consensus_injected_into_reconcile_prompt(monkeypatch):
    """D2a 的输出必须作为附加证据进入 reconcile（fail-open 无新硬闸）。"""
    conv = _conv()
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_judge(system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return {"decision": "finalize", "reason": "ok"}

    import app.services.agentic_judge as aj
    monkeypatch.setattr(aj, "judge_json", fake_judge)
    verdict, _reason = _run(mgr._reconcile_completion(
        "回复", "judge 说完成", {"status": "partial", "issues": []},
        [{"id": "s2", "description": "未完"}],
        consensus_findings={
            "unsupported_criteria": ["验收标准A"],
            "unchallenged_claim": "声称已导出 PDF",
            "claim_verdict": "无直接证据",
        },
    ))
    assert verdict == "finalize"
    assert "共识挑战" in captured["prompt"] or "consensus" in captured["prompt"].lower()
    assert "声称已导出 PDF" in captured["prompt"]


def test_config_defaults_off_when_key_absent():
    """M2 空真修正（复审）：autouse fixture 会先 set True——必须显式 pop 再断言
    属性行为（缺 key → 默认关，D-960 保守）。"""
    from app.core.config import get_config
    cfg = get_config()
    dm = cfg._config.setdefault("deathmatch", {})
    p1 = dm.pop("consensus_challenger_enabled", "___absent___")
    p2 = dm.pop("reflection_retract_enabled", "___absent___")
    try:
        assert cfg.deathmatch_consensus_challenger_enabled is False
        assert cfg.deathmatch_reflection_retract_enabled is False
    finally:
        if p1 != "___absent___":
            dm["consensus_challenger_enabled"] = p1
        if p2 != "___absent___":
            dm["reflection_retract_enabled"] = p2
