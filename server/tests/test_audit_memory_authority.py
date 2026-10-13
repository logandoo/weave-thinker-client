"""TDD: injected memory + self-facts enter the pre-send audit evidence domain.

conv 3583d840 (2026-09-18, production 60 box): the model knew its repo URL
from injected long-term memory (its thinking trace said so) and draft 1
stated it correctly — but the auditor's evidence corpus contained ONLY tool
results (empty this turn), judged the claim "凭空编造", and explicitly
instructed the writer to say it could not provide the address. The refusal
shipped. User direction: memory priority is the highest; the clarification
system handles the rare wrong memory, so the audit must never override
memory-backed facts.

Run from backend/: ../.venv/bin/python -m pytest tests/test_audit_memory_authority.py -v
"""
import asyncio

from app.services.agent_loop import AgentLoop, AgentLoopState, _build_audit_evidence

MEM_FACT = "用户的开源项目 Weave Thinker 的代码仓库是 https://github.com/alice/weave-thinker"
REPO_URL = "https://github.com/alice/weave-thinker"


def _evidence(state):
    return asyncio.run(_build_audit_evidence(state))


class TestAuditEvidenceAuthority:
    def test_injected_memory_appears_in_ledger_and_corpus(self):
        state = AgentLoopState(messages=[])
        state.injected_memory_context = MEM_FACT
        ledger, text, full = _evidence(state)
        assert "[M]" in ledger
        assert MEM_FACT in text
        assert MEM_FACT in full
        # A4.9 I1：记忆是可被外力影响的文本，必须包裹为数据而非指令
        assert "不是指令" in text
        # Minor-2：声明 [S] 只进 display 不进 full 的分层
        assert "[S]" not in full

    def test_no_global_repo_fact_in_audit_evidence(self):
        # 用户纠正（2026-09-18）：全局审计证据不得携带仓库事实；[S] 仅保留
        # 产品身份（无 URL）——无记忆用户的 URL 声称不可借 [S] 放行。
        state = AgentLoopState(messages=[])
        state.injected_memory_context = ""
        ledger, text, full = _evidence(state)
        assert "[S]" in ledger
        assert "Weave Thinker" in text
        assert REPO_URL not in ledger
        assert REPO_URL not in text
        assert REPO_URL not in full
        assert "[M]" not in full

    def test_auditor_template_declares_memory_authority(self):
        tpl = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
        assert "[M]" in tpl
        assert "[S]" in tpl
        assert "以用户本轮消息为准" in tpl
        assert "不得指示写手" in tpl
