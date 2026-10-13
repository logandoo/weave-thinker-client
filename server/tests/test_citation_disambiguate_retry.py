"""Tests — citation disambiguate judge 超时配置化 + 重试（conv a040c24e）.

生产证据（服务器 <cloud-host>，2026-09-06 00:21:04 local，日志实证）：
`judge_json task=citation_disambiguate timed out after 20s` —— 单次 20s 超时
即 fail-open，越界 [11] 未剥除直接落库（msg baab73dc）。生产 deepseek 拥塞
时段 20s 不够，且无重试。

Required behavior:
  T1 sanitize_texts 的 judge 超时来自 config `[agent.citation]
     disambiguate_timeout_seconds`（默认 60.0，超时审核 wave 由 30 上调），不再是硬编码 20.0。
  T2 judge 失败（返回非 dict）时按 `[agent.citation] disambiguate_retries`
     （默认 1）重试；重试成功即按其结果剥除。
  T3 重试后仍失败 → 维持 fail-open（保留原文，文档化行为）。

Run: cd backend && python -m pytest tests/test_citation_disambiguate_retry.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import citation_ledger  # noqa: E402
from app.services.citation_ledger import CitationLedger  # noqa: E402


def _ledger_with_one_entry():
    ledger = CitationLedger()
    ledger.register("http://a.example/", title="A", snippet="s")
    return ledger


def test_t1_timeout_from_config_not_hardcoded():
    import inspect

    src = inspect.getsource(citation_ledger)
    assert "timeout=20.0" not in src, "disambiguate 超时仍硬编码 20.0"
    from app.core.config import get_config

    cfg = get_config()
    assert hasattr(cfg, "agent_citation_disambiguate_timeout"), "config 缺 agent_citation_disambiguate_timeout 属性"
    assert float(cfg.agent_citation_disambiguate_timeout) >= 25.0
    assert hasattr(cfg, "agent_citation_disambiguate_retries"), "config 缺 agent_citation_disambiguate_retries 属性"


def test_t2_retry_then_success(monkeypatch):
    calls = {"n": 0}

    async def fake_judge(system_prompt, user_prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 首次超时/失败
        return {"citation_ids": [11]}

    monkeypatch.setattr("app.services.agentic_judge.judge_json", fake_judge)
    ledger = _ledger_with_one_entry()
    texts = ["结论 [1]，越界引用 [11]。"]
    out = asyncio.run(ledger.sanitize_texts(texts, timeout=1.0, retries=1))
    assert calls["n"] == 2, f"未重试（calls={calls['n']}）"
    assert "[11]" not in out[0] and "[1]" in out[0]


def test_t3_all_retries_fail_failopen(monkeypatch):
    calls = {"n": 0}

    async def fake_judge(system_prompt, user_prompt, **kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr("app.services.agentic_judge.judge_json", fake_judge)
    ledger = _ledger_with_one_entry()
    texts = ["越界引用 [11] 保留。"]
    out = asyncio.run(ledger.sanitize_texts(texts, timeout=1.0, retries=1))
    assert calls["n"] == 2
    assert out[0] == texts[0], "fail-open 语义被破坏（原文被改动）"


def test_t3_zero_retries_single_attempt(monkeypatch):
    calls = {"n": 0}

    async def fake_judge(system_prompt, user_prompt, **kw):
        calls["n"] += 1
        return None

    monkeypatch.setattr("app.services.agentic_judge.judge_json", fake_judge)
    ledger = _ledger_with_one_entry()
    asyncio.run(ledger.sanitize_texts(["x [11]"], timeout=1.0, retries=0))
    assert calls["n"] == 1, f"retries=0 时应只调用 1 次（calls={calls['n']}）"
