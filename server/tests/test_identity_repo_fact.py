"""TDD: self-fact knowledge boundary for the assistant's own repository.

User correction (2026-09-18, conv 3583d840 follow-up): the repo address was
legitimately known ONLY because the user's long-term memory carries it. A
global system-prompt fact is wrong — users without that memory must not know
it. Correct contract: the system prompt carries NO repo URL; self technical
information is answered from the current context (memory/materials) only, and
with no such info the assistant states its memory lacks it and offers a web
search.

Run from backend/: ../.venv/bin/python -m pytest tests/test_identity_repo_fact.py -v
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
    workspace = SimpleNamespace(root_path="/tmp/wt-identity-test")
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


class TestIdentityRepoBoundary:
    def test_prompt_carries_no_repo_url(self):
        assert REPO_URL not in _build_prompt()

    def test_prompt_directs_context_boundary_and_search_fallback(self):
        prompt = _build_prompt()
        assert "只依据当前上下文" in prompt
        assert "记忆中目前没有这个信息" in prompt
        assert "联网检索" in prompt
        # A4.9 I1 follow-up: memory wins over a search miss (run-2 retraction)
        assert "以记忆为准直接采用" in prompt

    def test_identity_tool_ban_has_search_fallback_exception(self):
        # A4.9 I2：4b 提议联网检索，但规则 7 禁止身份问题调工具——必须给
        # 明确要求检索的场景开例外，否则用户说「好，帮我搜」会被规则 7 拒绝。
        prompt = _build_prompt()
        assert "用户又明确要求联网检索" in prompt

    def test_model_name_secrecy_rules_intact(self):
        assert "严禁提及底层模型的具体名称" in _build_prompt()

    def test_weave_thinker_identity_rule_intact(self):
        assert "Weave Thinker 就是你自己" in _build_prompt()
