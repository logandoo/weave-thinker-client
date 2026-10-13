"""TDD: visible reasoning must not narrate meta references to the system prompt.

Production (conv 3583d840, after the self-fact deploy): the assistant's
user-visible thinking said "According to my system prompt, the authoritative
repo is ... — I should give that directly without any tool calls" and quoted
another prompt rule ("The system prompt says: ..."). The product rule already
says internal system prompts must not be exposed; the thinking trace is
user-visible, so the prompt must (a) frame identity facts as self-owned
knowledge and (b) forbid meta references in reasoning and answers.

Run from backend/: ../.venv/bin/python -m pytest tests/test_thinking_hygiene.py -v
"""
import asyncio
from types import SimpleNamespace

from app.services.agent_service import AgentService
from app.services.memory_service import AgentSharedContext

REPO_URL = "https://github.com/alice/weave-thinker"


def _build_prompt() -> str:
    service = AgentService()
    shared = AgentSharedContext(
        agent_state=None,
        memory_summary="",
        dream_summary="",
        memory_entries=[],
    )
    workspace = SimpleNamespace(root_path="/tmp/wt-thinkhygiene-test")
    return asyncio.run(
        service._build_system_prompt(
            assistant=None,
            shared_context=shared,
            workspace=workspace,
            user=None,
            identity_context="",
            deathmatch_mode=False,
        )
    )


class TestThinkingHygiene:
    def test_prompt_forbids_meta_references_in_reasoning(self):
        prompt = _build_prompt()
        assert "思考（reasoning，用户可见）" in prompt
        assert "system prompt" in prompt
        assert "内部规则" in prompt
        # A4.9 I1：变体引用（per identity rules 4b / the coordinator note says）
        # 也必须禁止——不能只禁字面“系统提示词”
        assert "内部规则编号" in prompt
        assert "协调器注记" in prompt
        assert "coordinator note" in prompt
        assert "除非用户明确要求" in prompt
        # A4.9 I1 round 2：模型会自引用规则本身（"注意不要提及系统提示词"）——
        # 明确禁止在思考中复述/转述内部指令措辞
        assert "不要在思考中复述" in prompt

    def test_canary_marker_hidden_from_reasoning(self):
        # canary 标记是内部校验；思考可见，模型不应在思考里讨论该标记
        from app.services.canary_marker import canary_prompt_section
        section = canary_prompt_section("CANARY123")
        assert "不要在思考" in section
        assert "CANARY123" in section

    def test_prompt_has_no_global_repo_fact(self):
        # 用户纠正（2026-09-18）：仓库事实不得全局注入——知识只来自该用户记忆
        assert REPO_URL not in _build_prompt()
