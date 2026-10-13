"""TDD: the coordinator pre-pass must not adjudicate self-info questions.

conv 3583d840 follow-up: the coordinator LLM (which cannot see the user's
shared long-term memory) generated focus guidance like "if no public repo
address, say unable to provide, don't fabricate links" for the question
"你自己的代码的地址吧，仓库地址吧。". The writer then received contradictory
inputs (memory HAS the fact vs coordinator says refuse) — visible reasoning
narrated the conflict, and one run searched the web, failed to find the repo,
and retracted the memory-backed answer.

Fix: coordinator = intent restatement only. For self technical-info questions
it must not judge whether the info exists and must not emit refusal
instructions; the main agent decides from its own memory/materials.

Run from backend/: ../.venv/bin/python -m pytest tests/test_coordinator_self_info_neutrality.py -v
"""
from app.services.agent_loop import AgentLoop


class TestCoordinatorSelfInfoNeutrality:
    def test_self_info_clause_defers_to_main_agent(self):
        tpl = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
        assert "你看不到主代理的共享长期记忆" in tpl

    def test_focus_must_not_carry_refusal_judgments(self):
        tpl = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
        assert "严禁在 focus 中" in tpl
        assert "不要编造链接" in tpl  # named in the prohibited guidance examples
