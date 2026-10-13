"""RED tests (A4.8) — LLM 判断类超时审核（2026-09-10，用户指令：LLM 判断不该过短）.

事故源：citation_disambiguate judge 硬编码 20s 在生产拥塞下超时 fail-open
（conv a040c24e，已修 30s）。用户裁决：全系统 LLM 判断类超时不应过短——
后台判断 ≥120s，持久化路径判断 ≥60s；实时语音判断为设计例外（慢=跳过）。

Required behavior:
  T1 judge_json 的默认超时从 config `[agent.auxiliary]
     judge_default_timeout_seconds` 解析（默认 ≥120s），不再硬编码 30s。
  T2 deathmatch 全部 judge 超时 ≥120s（DEFAULT_JUDGE_TIMEOUT +
     creative_goal/harness_repair/completion_reconcile 三调用点）。
  T3 citation disambiguate 默认 ≥60s（config 默认 + toml 值一致）。
  T4 指定 LLM 判断调用点（memory×2/skill_evolution/schedule/identity/
     error_classifier/title/multimodal/visual_critic）源码中不再存在
     timeout=15.0/20.0/25.0 短值。
  T5 docs/TIMEOUT_AUDIT.md 盘点文档存在且含分类表。

Run: cd backend && python -m pytest tests/test_timeout_audit.py -v
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── T1: judge_json default resolves from config, >=120s ──
def test_t1_judge_default_from_config():
    from app.core.config import get_config
    from app.services.agentic_judge import judge_json

    cfg = get_config()
    assert hasattr(cfg, "agent_auxiliary_judge_default_timeout"), "config 缺 agent_auxiliary_judge_default_timeout 属性"
    assert float(cfg.agent_auxiliary_judge_default_timeout) >= 120.0
    sig = inspect.signature(judge_json)
    default = sig.parameters["timeout"].default
    assert default is None or float(default) >= 120.0, f"judge_json 超时默认仍硬编码短值: {default}"


def test_t1_judge_timeout_resolution_uses_config(monkeypatch):
    """judge_json 不传 timeout 时必须解析到 config 值（而非模块常量）。"""
    import asyncio

    from app.services import agentic_judge

    captured = {}

    class FakeClient:
        def __init__(self, task="default", llm=None):
            pass

        async def complete_parts(self, messages, **kw):
            return ('{"ok": true}', "")

    monkeypatch.setattr(agentic_judge, "AuxiliaryClient", FakeClient)

    real_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, timeout):
        captured["timeout"] = timeout
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", spy_wait_for)
    out = asyncio.run(agentic_judge.judge_json("sys", "user"))
    assert out == {"ok": True}
    assert captured["timeout"] >= 120.0, f"judge_json 未传 timeout 时解析到 {captured['timeout']}"


# ── T2: deathmatch judge timeouts ──
def test_t2_deathmatch_judge_timeouts():
    from app.services import deathmatch_service

    assert deathmatch_service.DEFAULT_JUDGE_TIMEOUT >= 120.0, (
        f"DEFAULT_JUDGE_TIMEOUT={deathmatch_service.DEFAULT_JUDGE_TIMEOUT} < 120s"
    )
    src = inspect.getsource(deathmatch_service)
    for m in re.finditer(r'task="(creative_goal|harness_repair|completion_reconcile)"[^)]*?timeout=([0-9.]+)', src, re.DOTALL):
        assert float(m.group(2)) >= 120.0, f"{m.group(1)} judge timeout={m.group(2)} < 120s"


# ── T3: citation disambiguate default >=60s ──
def test_t3_citation_disambiguate_default():
    from app.core.config import get_config

    cfg = get_config()
    assert float(cfg.agent_citation_disambiguate_timeout) >= 60.0
    import tomllib

    with open(os.path.join(os.path.dirname(__file__), "..", "config.toml"), "rb") as f:
        toml_cfg = tomllib.load(f)
    assert float(toml_cfg["agent"]["citation"]["disambiguate_timeout_seconds"]) >= 60.0


# ── T4: no short timeouts remain at the audited LLM-judgment call sites ──
def test_t4_no_short_timeouts_at_llm_judgment_sites():
    import app.services.agent_service as agent_service
    import app.services.error_classifier as error_classifier
    import app.services.memory_multimodal_service as memory_multimodal
    import app.services.memory_retrieval_service as memory_retrieval
    import app.services.memory_subconscious_service as memory_subconscious
    import app.services.schedule_parser as schedule_parser
    import app.services.skill_evolution_service as skill_evolution
    import app.services.title_generator as title_generator
    import app.services.visual_critic as visual_critic

    mods = [
        agent_service, error_classifier, memory_multimodal, memory_retrieval,
        memory_subconscious, schedule_parser, skill_evolution, title_generator, visual_critic,
    ]
    offenders = []
    for mod in mods:
        src = inspect.getsource(mod)
        # A4.9 M4：不只守旧字面量——任何 <60s 的新短超时也不可引入这些
        # LLM 判断模块（整数毫秒值如 15000 不在此模式内）
        for m in re.finditer(r"timeout=([0-9]{1,2}\.[0-9]+|[0-9]{1,2})\b", src):
            if float(m.group(1)) < 60.0:
                line = src[:m.start()].count("\n") + 1
                offenders.append(f"{mod.__name__}:{line} timeout={m.group(1)}")
    assert not offenders, f"LLM 判断调用点仍存在 <60s 短超时: {offenders}"


# ── T5: audit doc exists with the classification table ──
def test_t5_audit_doc_exists():
    doc = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "TIMEOUT_AUDIT.md")
    assert os.path.exists(doc)
    content = open(doc, encoding="utf-8").read()
    assert "L-BG" in content and "L-PERSIST" in content and "L-RT" in content
