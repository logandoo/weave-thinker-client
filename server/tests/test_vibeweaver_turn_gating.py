"""W17 vibeweaver 轮次门控 — 钩子 stand-down 测试（TDD RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_vibeweaver_turn_gating.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services.vibeweaver_turn_gate import (
    explicit_trigger_hit,
    coding_heuristic,
    resolve_turn_active,
)


class TestExplicitTrigger:
    def test_plain_mention_case_insensitive(self):
        assert explicit_trigger_hit("严格遵守 Vibeweaver 完成这个任务") is True

    def test_slash_invocation(self):
        assert explicit_trigger_hit("/vibeweaver 帮我写一篇文章") is True

    def test_skill_bracket_invocation(self):
        assert explicit_trigger_hit("[skill:vibeweaver] 整理这份文档") is True

    def test_no_mention(self):
        assert explicit_trigger_hit("帮我写一篇关于春天的散文") is False


class TestCodingHeuristic:
    @pytest.mark.parametrize("text", [
        "帮我修复这个登录接口的 bug",
        "写一个 python 脚本批量重命名文件",
        "这段代码报 NullPointerException，帮我看看堆栈",
        "用 docker 部署这个项目",
        "给这个函数补单元测试",
        "npm install 报错怎么办",
        "重构这个 vue 组件的状态管理",
        "帮我写个 SQL 查询统计日活",
    ])
    def test_coding_true(self, text):
        assert coding_heuristic(text) is True

    @pytest.mark.parametrize("text", [
        "帮我写一篇关于春天的散文",
        "为《掼蛋宝典》配置20个经典案例，以WORD文件输出",
        "怎样关闭华为手机的屏幕录屏",
        "翻译这句话成英文：Hello world",
        "解释一下什么是量子纠缠",
        "给我讲讲三国时期的历史",
    ])
    def test_coding_false(self, text):
        assert coding_heuristic(text) is False

    def test_empty_is_false(self):
        assert coding_heuristic("") is False


class TestResolveTurnActive:
    def test_explicit_wins_over_false_coding(self):
        assert resolve_turn_active("严格遵守 vibeweaver 写一篇文章", False) is True

    def test_coding_true_activates(self):
        assert resolve_turn_active("修复这个 bug", True) is True

    def test_coding_false_deactivates(self):
        assert resolve_turn_active("写一篇散文", False) is False

    def test_none_falls_back_to_heuristic_true(self):
        assert resolve_turn_active("帮我调试这段 python 代码", None) is True

    def test_none_falls_back_to_heuristic_false(self):
        assert resolve_turn_active("以案例讲解掼蛋打法，输出 Word 文档", None) is False


import asyncio


def _run(coro):
    return asyncio.run(coro)


class TestForceInjectSplit:
    """skill_tools 注入拆分：目录恒在，force-inject 可关。"""

    def _clear_cache(self):
        from app.tools import skill_tools
        skill_tools._SKILL_PROMPT_CACHE.clear()

    def test_catalog_only_has_no_force_block(self):
        self._clear_cache()
        from app.tools.skill_tools import build_skills_system_prompt
        out = _run(build_skills_system_prompt(None, include_force_inject=False))
        self._clear_cache()
        assert "<force_injected_skill" not in out
        assert "vibeweaver" in out  # 目录仍列出（可 skill_view 自触发）

    def test_include_force_inject_matches_legacy_shape(self):
        self._clear_cache()
        from app.tools.skill_tools import (
            build_skills_system_prompt,
            build_force_injected_skills_prompt,
        )
        full = _run(build_skills_system_prompt(None, include_force_inject=True))
        self._clear_cache()
        block = _run(build_force_injected_skills_prompt(None))
        catalog = _run(build_skills_system_prompt(None, include_force_inject=False))
        self._clear_cache()
        assert '<force_injected_skill name="vibeweaver">' in full
        assert '<force_injected_skill name="vibeweaver">' in block
        # 拆分可重组：目录 + 注入段 == 旧一体输出（注入段自带 authoritative 头）
        assert full.startswith(catalog)
        assert full.endswith(block.strip()) or block.strip() in full


def _bare_loop():
    from app.services.agent_loop import AgentLoop
    loop = AgentLoop.__new__(AgentLoop)
    loop.deathmatch_manager = None
    loop._identity_context = ""
    loop._last_coord_messages = []
    return loop


class TestCoordinatorCodingTask:
    """coordinator 输出契约：coding_task 三态（true/false/None）。"""

    def _patch(self, monkeypatch, loop, payload_json):
        async def fake(messages, temperature=0.3, **kw):
            return payload_json
        monkeypatch.setattr(loop, "_complete_json", fake)

    def test_true_parsed(self, monkeypatch):
        loop = _bare_loop()
        self._patch(monkeypatch, loop,
            '{"route":"tool_loop","expects_tools":true,"search_required":false,'
            '"creative_turn":false,"coding_task":true,"focus":"修 bug"}')
        r = _run(loop._coordinate([{"role": "user", "content": "修复这个 bug"}]))
        assert r["coding_task"] is True

    def test_false_parsed(self, monkeypatch):
        loop = _bare_loop()
        self._patch(monkeypatch, loop,
            '{"route":"tool_loop","expects_tools":true,"search_required":false,'
            '"creative_turn":false,"coding_task":false,"focus":"写文档"}')
        r = _run(loop._coordinate([{"role": "user", "content": "写 Word 文档"}]))
        assert r["coding_task"] is False

    def test_missing_is_none(self, monkeypatch):
        loop = _bare_loop()
        self._patch(monkeypatch, loop,
            '{"route":"tool_loop","expects_tools":true,"search_required":false,'
            '"creative_turn":false,"focus":"旧模板"}')
        r = _run(loop._coordinate([{"role": "user", "content": "随便"}]))
        assert r["coding_task"] is None

    def test_non_bool_is_none(self, monkeypatch):
        loop = _bare_loop()
        self._patch(monkeypatch, loop,
            '{"route":"tool_loop","expects_tools":true,"search_required":false,'
            '"creative_turn":false,"coding_task":"yes","focus":"怪值"}')
        r = _run(loop._coordinate([{"role": "user", "content": "怪值"}]))
        assert r["coding_task"] is None

    def test_truncated_salvage_keeps_coding_task(self, monkeypatch):
        loop = _bare_loop()
        self._patch(monkeypatch, loop,
            '{"route":"tool_loop","expects_tools":true,"search_required":false,'
            '"creative_turn":false,"coding_task":true,"focus":"这段话被截断了还没')
        r = _run(loop._coordinate([{"role": "user", "content": "部署服务"}]))
        assert r is not None and r["coding_task"] is True

    def test_template_contains_coding_task_field(self):
        from app.services.agent_loop import AgentLoop
        tpl = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
        assert '"coding_task"' in tpl
        assert "coding_task" in tpl


from app.services import hook_service as hs


class TestHookContextFields:
    def test_tool_ctx_defaults_active(self):
        ctx = hs.ToolHookContext(tool_name="workspace_write")
        assert ctx.vibeweaver_active is True

    def test_stop_ctx_defaults_active(self):
        ctx = hs.StopContext()
        assert ctx.vibeweaver_active is True


class TestGateStandDown:
    def _mk_ctx(self, tmp_path, active):
        ws = tmp_path / "ws"
        (ws / "proj" / "tests").mkdir(parents=True)
        # active 项目 = 含 tests/verification_log.md（find_project_root 判据）
        (ws / "proj" / "tests" / "verification_log.md").write_text("## t\n", encoding="utf-8")
        return hs.ToolHookContext(
            tool_name="workspace_write",
            args={"file_path": str(ws / "proj" / "a.md")},
            conversation_id="conv-standdown",
            workspace_path=str(ws),
            vibeweaver_active=active,
        )

    def test_inactive_skips_gate_check(self, tmp_path, monkeypatch):
        from app.services import vibeweaver_gate_service as g
        calls = []

        async def fake_check(root, file_path):
            calls.append(root)
            return {"block": "GATE-BLOCKED simulated"}
        monkeypatch.setattr(g, "gate_check_write_async", fake_check)
        out = _run(g._tool_after_hook(self._mk_ctx(tmp_path, False)))
        assert out is None and calls == []

    def test_active_still_blocks(self, tmp_path, monkeypatch):
        from app.services import vibeweaver_gate_service as g
        calls = []

        async def fake_check(root, file_path):
            calls.append(root)
            return {"block": "GATE-BLOCKED simulated"}
        monkeypatch.setattr(g, "gate_check_write_async", fake_check)
        out = _run(g._tool_after_hook(self._mk_ctx(tmp_path, True)))
        assert out is not None and out.block and calls

    def test_stop_inactive_skips_idle_check(self, tmp_path, monkeypatch):
        from app.services import vibeweaver_gate_service as g
        calls = []

        async def fake_idle(root):
            calls.append(root)
            return {"blocking": ["missing evidence"]}
        monkeypatch.setattr(g, "gate_idle_check_async", fake_idle)
        ws = tmp_path / "ws2"
        (ws / "proj").mkdir(parents=True)
        ctx = hs.StopContext(
            conversation_id="conv-standdown-stop",
            workspace_path=str(ws),
            vibeweaver_active=False,
        )
        out = _run(g._stop_hook(ctx))
        assert out is None and calls == []


class TestAuditStandDown:
    def _red_root(self, tmp_path, conv):
        from app.services.vibeweaver_audit_service import latch_red
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True, exist_ok=True)
        (root / "tests" / "verification_log.md").write_text("## t\n", encoding="utf-8")
        latch_red(root, conv, ["simulated bad audit"])
        return root

    def _mk_ctx(self, root, active):
        return hs.ToolHookContext(
            tool_name="workspace_write",
            args={"file_path": str(root / "out.md")},
            conversation_id="conv-latch",
            workspace_path=str(root),
            vibeweaver_active=active,
        )

    def test_latch_blocks_when_active(self, tmp_path):
        from app.services import vibeweaver_audit_service as a
        root = self._red_root(tmp_path, "conv-latch")
        out = _run(a._audit_tool_before(self._mk_ctx(root, True)))
        assert out is not None and out.block and "RED-LATCHED" in out.block

    def test_latch_stand_down_when_inactive(self, tmp_path):
        from app.services import vibeweaver_audit_service as a
        root = self._red_root(tmp_path, "conv-latch")
        out = _run(a._audit_tool_before(self._mk_ctx(root, False)))
        assert out is None

    def test_stop_claim_check_stand_down_when_inactive(self, tmp_path, monkeypatch):
        from app.services import vibeweaver_audit_service as a
        root = tmp_path / "proj"
        root.mkdir(parents=True, exist_ok=True)
        called = []

        def fake_audit(content, r):
            called.append(1)
            return {"verdict": "bad", "reasons": ["x"]}
        monkeypatch.setattr(a, "audit_final_output", fake_audit)
        ctx = hs.StopContext(
            conversation_id="conv-claim",
            workspace_path=str(root),
            assistant_content="[Verification Gate] ...",
            vibeweaver_active=False,
        )
        out = _run(a._audit_stop_hook(ctx))
        assert out is None and called == []
        red, _ = a.red_state(root, "conv-claim")
        assert red is False


class TestAgentLoopWiring:
    def test_init_sets_default_true(self):
        import inspect
        from app.services.agent_loop import AgentLoop
        src = inspect.getsource(AgentLoop.__init__)
        assert "self.vibeweaver_active" in src

    def test_hook_contexts_pass_through(self):
        import inspect
        from app.services.agent_loop import AgentLoop
        src = inspect.getsource(AgentLoop)
        assert src.count("vibeweaver_active=self.vibeweaver_active") >= 2


class TestEscapeHatch:
    """逃生口：gating_enabled=False 恢复无条件激活（判据 #6）。"""

    def test_gating_off_forces_active_even_noncoding(self):
        from app.services.vibeweaver_turn_gate import resolve_effective_active
        assert resolve_effective_active("帮我写一篇散文", False, gating_enabled=False) is True

    def test_gating_off_forces_active_even_coord_false(self):
        from app.services.vibeweaver_turn_gate import resolve_effective_active
        assert resolve_effective_active("随便聊聊", False, gating_enabled=False) is True

    def test_gating_on_defers_to_resolve(self):
        from app.services.vibeweaver_turn_gate import resolve_effective_active
        assert resolve_effective_active("帮我写一篇散文", False, gating_enabled=True) is False
        assert resolve_effective_active("用 vibeweaver 写散文", False, gating_enabled=True) is True

    def test_config_property_env_off_wins(self, monkeypatch):
        from app.core.config import Config
        cfg = object.__new__(Config)
        cfg._config = {"vibeweaver": {"task_gating_enabled": True}}
        monkeypatch.setenv("VIBEWEAVER_GATING", "off")
        assert cfg.vibeweaver_task_gating_enabled is False

    def test_config_property_key_false(self, monkeypatch):
        from app.core.config import Config
        cfg = object.__new__(Config)
        cfg._config = {"vibeweaver": {"task_gating_enabled": False}}
        monkeypatch.delenv("VIBEWEAVER_GATING", raising=False)
        assert cfg.vibeweaver_task_gating_enabled is False

    def test_config_property_default_true(self, monkeypatch):
        from app.core.config import Config
        cfg = object.__new__(Config)
        cfg._config = {"vibeweaver": {}}
        monkeypatch.delenv("VIBEWEAVER_GATING", raising=False)
        assert cfg.vibeweaver_task_gating_enabled is True


class TestContextFallback:
    """P2：coordinator 缺位时启发式兜底吃近 3 条用户消息 ctx；explicit 仍只看末条。"""

    def test_context_heuristic_activates_when_last_is_bare_followup(self):
        from app.services.vibeweaver_turn_gate import resolve_turn_active
        assert resolve_turn_active("继续处理", None, context_text="帮我重构这个 python 函数") is True

    def test_context_non_coding_stays_off(self):
        from app.services.vibeweaver_turn_gate import resolve_turn_active
        assert resolve_turn_active("继续", None, context_text="写一首关于秋天的小诗") is False

    def test_coord_false_beats_context(self):
        from app.services.vibeweaver_turn_gate import resolve_turn_active
        assert resolve_turn_active("写首诗", False, context_text="重构代码") is False

    def test_explicit_stays_last_message_only(self):
        from app.services.vibeweaver_turn_gate import resolve_turn_active
        assert resolve_turn_active("写一首诗", False, context_text="严格遵守 vibeweaver") is False

    def test_effective_passes_context_and_gating(self):
        from app.services.vibeweaver_turn_gate import resolve_effective_active
        assert resolve_effective_active("继续处理", None, True, context_text="重构代码") is True
        assert resolve_effective_active("继续处理", None, False, context_text="重构代码") is True

    def test_ctx_looseness_known_tradeoff_pinned(self):
        """明示宽松项（复审 MINOR-5）：coordinator 缺位 + 工程史 + 无关写作仍激活
        ——宁多勿漏；explicit-only-末条 与 coord bool 短路不受影响。"""
        from app.services.vibeweaver_turn_gate import resolve_turn_active
        assert resolve_turn_active("写首诗吧", None, context_text="重构代码") is True


class TestExtractRecentUserText:
    def test_basic_order_and_limit(self):
        from app.services.vibeweaver_turn_gate import extract_recent_user_text
        msgs = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "x"},
            {"role": "user", "content": "B"},
            {"role": "user", "content": "C"},
            {"role": "user", "content": "D"},
        ]
        last, ctx = extract_recent_user_text(msgs, limit=3)
        assert last == "D"
        assert ctx == "B\nC\nD"

    def test_skips_non_str_content(self):
        from app.services.vibeweaver_turn_gate import extract_recent_user_text
        msgs = [{"role": "user", "content": ["x"]}, {"role": "user", "content": "B"}]
        last, ctx = extract_recent_user_text(msgs)
        assert last == "B" and ctx == "B"

    def test_empty(self):
        from app.services.vibeweaver_turn_gate import extract_recent_user_text
        assert extract_recent_user_text([]) == ("", "")

    def test_limit_non_positive_returns_empty(self):
        from app.services.vibeweaver_turn_gate import extract_recent_user_text
        msgs = [{"role": "user", "content": "A"}, {"role": "user", "content": "B"}]
        assert extract_recent_user_text(msgs, limit=0) == ("", "")
        assert extract_recent_user_text(msgs, limit=-3) == ("", "")


class TestP1P3Wiring:
    def test_worker_sets_gate(self):
        import inspect
        import app.services.agent_worker as w
        src = inspect.getsource(w)
        assert "agent_loop.vibeweaver_active = _vw_resolve_eff(" in src
        assert "vibeweaver turn gate active=%s explicit=%s coding=None gating=%s task=%s" in src

    def test_scheduler_sets_gate(self):
        import inspect
        import app.services.agent_scheduler as s
        src = inspect.getsource(s)
        assert "loop.vibeweaver_active = _vw_resolve_eff(" in src
        assert "vibeweaver turn gate active=%s explicit=%s coding=None gating=%s task=%s" in src

    def test_chat_passes_context_and_extracts_before_deathmatch(self):
        import inspect
        import app.api.chat as c
        import app.api.chat_stream_transport as t
        src = inspect.getsource(c) + inspect.getsource(t)
        assert "_vw_text, _vw_ctx = _vw_extract(messages_for_model)" in src
        assert "context_text=_vw_ctx" in src
        # 复审 IMPORTANT-1：抽取必须先于 deathmatch 合成注入（跨轮摘要/计划指令
        # 均为 user 角色，混入 ctx 会让非代码死磕轮伪激活）。
        assert src.index("_vw_text, _vw_ctx = _vw_extract(messages_for_model)") < src.index(
            "build_context_messages(messages_for_model)"
        )
