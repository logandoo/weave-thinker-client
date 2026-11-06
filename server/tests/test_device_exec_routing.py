"""W16 设备执行模式 — remote_dispatch_router 单元测试（TDD RED 先行）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_device_exec_routing.py -v
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.services import remote_dispatch_router as rdr


class FakeConversation:
    def __init__(self, execution_target=None):
        self.execution_target = execution_target


class FakeUser:
    id = "uid-0123456789ab"
    username = "tester"


def _quiet_audit(monkeypatch):
    async def fake_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(rdr, "record_exec_audit", fake_audit)


class TestResolveExecutionTarget:
    def test_default_none(self):
        assert rdr.resolve_execution_target({}) is None

    def test_from_conversation(self):
        conv = FakeConversation("device:abc123")
        assert rdr.resolve_execution_target({"conversation": conv}) == "device:abc123"

    def test_explicit_kwarg_wins(self):
        conv = FakeConversation("device:abc123")
        got = rdr.resolve_execution_target(
            {"conversation": conv, "execution_target": "device:zzz"}
        )
        assert got == "device:zzz"

    def test_detached_conversation_falls_back(self):
        class Boom:
            @property
            def execution_target(self):
                raise RuntimeError("detached")

        assert rdr.resolve_execution_target({"conversation": Boom()}) is None


class TestStripInternalArgs:
    def test_permission_granted_stripped(self):
        args = {"path": "a.txt", "content": "x", "_permission_granted": True}
        out = rdr.strip_internal_args(args)
        assert "_permission_granted" not in out
        assert out["path"] == "a.txt"

    def test_original_not_mutated(self):
        args = {"_permission_granted": True}
        rdr.strip_internal_args(args)
        assert "_permission_granted" in args


class TestToolRouting:
    def test_server_owned_detection(self):
        assert rdr.is_server_owned("web_search") is True
        assert rdr.is_server_owned("session_search") is True
        assert rdr.is_server_owned("memory") is True
        assert rdr.is_server_owned("mcp-any-tool") is True
        assert rdr.is_server_owned("terminal") is False

    def test_cloud_target_is_local(self):
        async def run():
            return await rdr.try_remote_dispatch("terminal", {"command": "ls"}, {})

        assert asyncio.run(run()) is None

    def test_device_target_remote_allowed(self, monkeypatch):
        calls = {}

        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            calls["device_id"] = device_id
            calls["method"] = method
            calls["params"] = params
            return {"ok": True, "content": "OUT"}

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal",
                {"command": "ls", "_permission_granted": True},
                {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            )

        out = asyncio.run(run())
        assert out == "OUT"
        assert calls["device_id"] == "abc"
        assert calls["method"] == "tools/call"
        assert "_permission_granted" not in calls["params"]["args"]
        assert calls["params"]["tool"] == "terminal"

    def test_remote_permission_result_has_cloud_created_provenance(self, monkeypatch):
        audit_calls = []

        async def fake_audit(*args, **_kwargs):
            audit_calls.append(args)

        monkeypatch.setattr(rdr, "record_exec_audit", fake_audit)

        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            return {"ok": True, "content": '{"_permission_needed": true}'}

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls"},
                {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            )

        out = asyncio.run(run())
        assert getattr(out, "_remote_execution", False) is True
        assert audit_calls[-1][3] is False
        assert audit_calls[-1][4] == "permission-needed"

    def test_grant_is_sibling_of_args_and_initial_calls_omit_it(self, monkeypatch):
        _quiet_audit(monkeypatch)
        calls = []

        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            calls.append(params)
            return {"ok": True, "content": "OUT"}

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)
        kwargs = {
            "conversation": FakeConversation("device:abc"),
            "user": FakeUser(),
        }
        grant = {"version": 1, "nonce": "n1", "exp": 123, "signature": "abc"}

        async def initial():
            return await rdr.try_remote_dispatch("terminal", {"command": "ls"}, kwargs)

        async def approved():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls", "_permission_granted": True},
                {**kwargs, "_device_grant": grant},
            )

        assert asyncio.run(initial()) == "OUT"
        assert "grant" not in calls[-1]
        assert asyncio.run(approved()) == "OUT"
        assert calls[-1]["grant"] == grant
        assert calls[-1]["args"] == {"command": "ls"}
        assert "grant" not in calls[-1]["args"]

    def test_grant_bearing_skill_script_fails_before_rpc(self, monkeypatch):
        _quiet_audit(monkeypatch)
        calls = []

        async def fake_rpc(*args, **kwargs):
            calls.append((args, kwargs))
            return {"ok": True, "content": "SHOULD-NOT-EXECUTE"}

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"skill_run_script"})
        grant = {"version": 1, "nonce": "n1", "exp": 123, "signature": "abc"}

        async def run():
            return await rdr.try_remote_dispatch(
                "skill_run_script", {"skill_id": "s1", "script_name": "x.py"},
                {
                    "conversation": FakeConversation("device:abc"),
                    "user": FakeUser(),
                    "_device_grant": grant,
                },
            )

        out = asyncio.run(run())
        assert "签名未覆盖脚本内容" in out
        assert calls == []

    def test_device_target_server_owned_runs_local(self, monkeypatch):
        async def run():
            return await rdr.try_remote_dispatch(
                "web_search", {"query": "x"},
                {"conversation": FakeConversation("device:abc")},
            )

        assert asyncio.run(run()) is None

    def test_device_target_unlisted_tool_fails_closed(self, monkeypatch):
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})

        async def run():
            return await rdr.try_remote_dispatch(
                "browser_navigate", {"url": "http://x"},
                {"conversation": FakeConversation("device:abc")},
            )

        out = asyncio.run(run())
        assert out is not None
        assert "暂不可用" in out or "error" in out.lower()

    def test_offline_returns_friendly_error(self, monkeypatch):
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            raise rdr.DeviceOfflineError("offline")

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls"},
                {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            )

        out = asyncio.run(run())
        assert out is not None
        assert "离线" in out

    def test_timeout_returns_error(self, monkeypatch):
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            raise rdr.DeviceCallTimeoutError("t")

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls"},
                {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            )

        out = asyncio.run(run())
        assert out is not None
        assert "超时" in out

    def test_disabled_feature_fail_closed(self, monkeypatch):
        # A4.9 I-8 / D-8：目标=设备且 feature 关闭 → 报错，不静默落回云端
        monkeypatch.setattr(rdr, "_feature_enabled", lambda: False)

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls"},
                {"conversation": FakeConversation("device:abc")},
            )

        out = asyncio.run(run())
        assert out is not None
        assert "停用" in out


class TestInternalArgStrip:
    def test_prefix_strip_not_exact_key(self):
        # A4.9 M1：所有 `_`-前缀内部键都不得过境（非仅 _permission_granted）
        args = {"path": "a", "_permission_granted": True, "_progress_cb": "x", "_foo": 1}
        out = rdr.strip_internal_args(args)
        assert out == {"path": "a"}


class TestToolClassification:
    """W18 G1/G2：矩阵三态补全（PLAN §7 对齐）——D-8：有本地 FS/进程副作用的
    工具不得在设备会话静默落云端执行。"""

    def test_mcp_underscore_is_server_owned(self):
        # W16 盲区：注册名为 mcp_{server}_{tool}（下划线），旧匹配只认 mcp-
        assert rdr.is_server_owned("mcp_remote_search") is True
        assert rdr.is_server_owned("mcp-x-y") is True

    def test_skill_library_tools_server_owned(self):
        assert rdr.is_server_owned("skill_view") is True
        assert rdr.is_server_owned("skill_manage") is True

    def test_workspace_tools_routed_remote(self):
        for name in ("citation_render", "pdf_export"):
            assert name in rdr.DEFAULT_REMOTE_ALLOWED_TOOLS, f"{name} 应归远端白名单"
            assert rdr.is_server_owned(name) is False

    def test_job_and_browser_deferred_fail_closed(self):
        # job_*（durable 进程）与 browser*（本机 chromium）在设备会话=暂缓拒绝，
        # 不得当 server-owned 本地执行（D-8 错误环境写）
        assert rdr.is_server_owned("job_submit") is False
        assert rdr.is_server_owned("browser") is False
        assert rdr.is_server_owned("browser_navigate") is False

    def test_job_deferred_rejected_in_device_mode(self):
        async def run():
            return await rdr.try_remote_dispatch(
                "job_submit", {"command": "ls"},
                {"conversation": FakeConversation("device:abc")},
            )

        out = asyncio.run(run())
        assert out is not None
        assert "暂缓" in out

    def test_deferred_message_distinct_from_unknown(self):
        # A4.9 M1：DEFERRED 分类有专属文案（不再借道通用「暂不可用」）
        assert rdr.is_deferred("job_submit") is True
        assert rdr.is_deferred("browser_navigate") is True
        assert rdr.is_deferred("browser_video") is True
        assert rdr.is_deferred("workspace_snapshot") is True
        assert rdr.is_deferred("terminal") is False


class TestConfigDriftWarn:
    def test_missing_defaults_warn_once(self, monkeypatch, caplog):
        # A4.9 I-4：旧部署 config 白名单缺 DEFAULT 新成员 → 一次性告警
        import logging

        monkeypatch.setattr(rdr, "_ALLOWED_TOOLS_DRIFT_WARNED", False)
        monkeypatch.setattr(
            rdr, "_remote_allowed_tools",
            rdr._remote_allowed_tools,  # keep real fn; drive via config monkeypatch
        )
        from app.core.config import get_config

        cfg = get_config()
        prev = cfg._config.get("client_exec", {}).get("allowed_tools")
        try:
            cfg._config.setdefault("client_exec", {})["allowed_tools"] = ["terminal"]
            with caplog.at_level(logging.WARNING, logger="app.services.remote_dispatch_router"):
                s1 = rdr._remote_allowed_tools()
                s2 = rdr._remote_allowed_tools()
            assert s1 == frozenset({"terminal"})
            warns = [r for r in caplog.records if "allowed_tools 缺少默认远端工具" in r.getMessage()]
            assert len(warns) == 1, f"应恰好告警一次，实际 {len(warns)}"
        finally:
            cfg._config.setdefault("client_exec", {})["allowed_tools"] = prev


class TestWorkspaceCtxFailClosed:
    """P1 ③（对抗评审 r1-F2）：workspace_ctx 算不出即不派发（fail-closed），
    params 永不携带 null/空 workspace_ctx 键（设备侧新名门键在但空=拒绝）。
    TDD RED 先行（2026-10-12 handoff 波）。"""

    def _dispatch(self, monkeypatch, kwargs):
        calls = {}

        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            calls["params"] = params
            return {"ok": True, "content": "OUT"}

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)

        async def run():
            return await rdr.try_remote_dispatch(
                "terminal", {"command": "ls"}, kwargs
            )

        out = asyncio.run(run())
        return out, calls

    def test_uncomputable_workspace_ctx_fails_closed_no_dispatch(self, monkeypatch):
        out, calls = self._dispatch(
            monkeypatch, {"conversation": FakeConversation("device:abc")}
        )
        assert "params" not in calls, "workspace_ctx 不可算仍派发了 tools/call（fail-open）"
        assert out is not None and ("workspace" in out.lower() or "身份" in out)

    def test_fail_closed_emits_warning_log(self, monkeypatch, caplog):
        # 评审 A-I2：user 缺失主触发面必须在日志留痕（审计行因无 user_id 不落库）
        import logging

        with caplog.at_level(
            logging.WARNING, logger="app.services.remote_dispatch_router"
        ):
            out, calls = self._dispatch(
                monkeypatch, {"conversation": FakeConversation("device:abc")}
            )
        assert "params" not in calls
        assert any("fail-closed" in rec.message for rec in caplog.records), (
            "fail-closed 路径必须 logger.warning 留痕"
        )

    def test_empty_user_id_workspace_ctx_fails_closed(self, monkeypatch):
        class NoIdUser:
            id = ""
            username = "tester"

        out, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": NoIdUser()},
        )
        assert "params" not in calls, "user_id 为空仍派发（名门期望值无法成立）"
        assert out is not None

    def test_workspace_ctx_compute_exception_fails_closed(self, monkeypatch):
        import app.services.workspace_service as ws

        def boom(value):
            raise RuntimeError("slugify down")

        monkeypatch.setattr(ws, "_slugify", boom)
        out, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
        )
        assert "params" not in calls, "计算异常仍派发（fail-open）"
        assert out is not None

    def test_workspace_ctx_sent_when_computable(self, monkeypatch):
        out, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
        )
        assert out == "OUT"
        assert calls["params"]["workspace_ctx"] == "tester-uid-0123"
        assert calls["params"]["workspace_ctx"] is not None


class TestIdentityUnboundConsumption:
    """P1 ④：设备侧错误码 identity_unbound（result.error 前缀，tools/fs 同形）→
    云端映射友好提示，与 oversize「超过」分类同款。TDD RED 先行（2026-10-12）。"""

    HINT = "设备未绑定执行身份，请在客户端重新配对设备后重试。"

    def _dispatch(self, monkeypatch, kwargs, fake_rpc=None, fake_fetch=None):
        if fake_rpc is not None:
            monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        if fake_fetch is not None:
            monkeypatch.setattr(rdr, "fetch_file_bytes", fake_fetch, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)

        async def run(tool, args):
            return await rdr.try_remote_dispatch(tool, args, kwargs)

        return run

    def test_identity_unbound_result_mapped_to_friendly_hint(self, monkeypatch):
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            return {"ok": False, "error": "identity_unbound: no bound cloud identity"}

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_rpc=fake_rpc,
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out is not None
        assert self.HINT in out
        assert "identity_unbound" not in out

    def test_identity_unbound_on_byte_fetch_path_mapped(self, monkeypatch):
        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            raise rdr.DeviceCallError("identity_unbound: no bound cloud identity")

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_fetch=fake_fetch,
        )
        out = asyncio.run(run("vision_interpret", {"image_path": "a.png"}))
        assert out is not None
        assert self.HINT in out
        assert "identity_unbound" not in out

    def test_oversize_error_classification_unchanged(self, monkeypatch):
        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            raise rdr.DeviceCallError("超过 10MB 上限")

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_fetch=fake_fetch,
        )
        out = asyncio.run(run("vision_interpret", {"image_path": "a.png"}))
        assert out is not None
        assert "过大" in out

    def test_unknown_error_passthrough_unchanged(self, monkeypatch):
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            return {"ok": False, "error": "workspace_mismatch: ctx a != b"}

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_rpc=fake_rpc,
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out is not None
        assert "workspace_mismatch" in out

    def test_identity_unbound_envelope_device_call_error_mapped(self, monkeypatch):
        # R2 NEW-m1：tools/call JSON-RPC 信封形态（DeviceCallError 冒出）同码同映射
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            raise rdr.DeviceCallError("identity_unbound: no bound cloud identity")

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_rpc=fake_rpc,
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out is not None
        assert self.HINT in out
        assert "identity_unbound" not in out

    def test_envelope_unknown_error_keeps_old_format(self, monkeypatch):
        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            raise rdr.DeviceCallError("boom: something failed")

        run = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            fake_rpc=fake_rpc,
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out is not None
        assert "设备执行失败: boom: something failed" in out


class TestWireErrorHardening:
    """遗留清偿 #2/#3：fs-proxy 消费面 + 结构化 error_code 优先 + 闭码集防伪装。"""

    HINT = "设备未绑定执行身份，请在客户端重新配对设备后重试。"

    def test_classify_prefers_structured_code(self):
        # 伪装腿：错误文本以 identity_unbound: 开头但结构化码=workspace_unavailable
        # → 结构化优先，不误触提示
        from app.services.client_device_service import classify_wire_error

        got = classify_wire_error(
            "identity_unbound: spoofed tool output", code="workspace_unavailable"
        )
        assert got is None, "结构化 code 覆盖前缀，伪装串不得触发提示"

    def test_classify_structured_identity_unbound_maps(self):
        from app.services.client_device_service import classify_wire_error

        assert classify_wire_error("whatever", code="identity_unbound") == self.HINT

    def test_classify_legacy_prefix_fallback(self):
        from app.services.client_device_service import classify_wire_error

        assert classify_wire_error("identity_unbound: no bound identity") == self.HINT

    def test_classify_contains_but_not_code_no_fire(self):
        from app.services.client_device_service import classify_wire_error

        assert classify_wire_error("workspace_mismatch: identity_unbound was expected") is None
        assert classify_wire_error("identity_unboundXYZ: y") is None

    def test_classify_unknown_code_passthrough(self):
        from app.services.client_device_service import classify_wire_error

        assert classify_wire_error("boom: something failed") is None


class TestDeviceOwnerFence:
    """C1（评审 r3/r4 Critical）：派发属主栅栏——device_id 被他人抢占时拒绝派发。"""

    def _dispatch(self, monkeypatch, kwargs, sess_user):
        calls = {}

        async def fake_rpc(device_id, method, params, timeout=None, expected_user_id=None):
            calls["params"] = params
            return {"ok": True, "content": "OUT"}

        async def fake_fetch(device_id, method, params, max_bytes=None, expected_user_id=None):
            calls["fetched"] = True
            return b"x"

        monkeypatch.setattr(rdr, "rpc_call", fake_rpc, raising=False)
        monkeypatch.setattr(rdr, "fetch_file_bytes", fake_fetch, raising=False)
        monkeypatch.setattr(rdr, "_remote_allowed_tools", lambda: {"terminal"})
        monkeypatch.setattr(rdr, "_call_timeout", lambda: 30)
        from app.services.device_session_service import device_session_manager

        monkeypatch.setattr(
            device_session_manager, "device_user_id", lambda did: sess_user
        )

        async def run(tool, args):
            return await rdr.try_remote_dispatch(tool, args, kwargs)

        return run, calls

    def test_foreign_session_owner_blocked(self, monkeypatch):
        run, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            sess_user="attacker-uid-9999",
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out is not None and "安全拦截" in out
        assert "params" not in calls, "跨属主会话不得派发（劫持通道）"

    def test_foreign_session_owner_blocks_byte_path_too(self, monkeypatch):
        run, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            sess_user="attacker-uid-9999",
        )
        out = asyncio.run(run("vision_interpret", {"image_path": "a.png"}))
        assert out is not None and "安全拦截" in out
        assert "fetched" not in calls

    def test_same_owner_dispatches(self, monkeypatch):
        run, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc"), "user": FakeUser()},
            sess_user="uid-0123456789ab",
        )
        out = asyncio.run(run("terminal", {"command": "ls"}))
        assert out == "OUT"
        assert calls["params"]["workspace_ctx"] == "tester-uid-0123"

    def test_byte_path_missing_user_fails_closed(self, monkeypatch):
        # NEW-I1（r4）：字节面缺用户上下文不得派发（无 ws_ctx 兜底的 fail-closed）
        run, calls = self._dispatch(
            monkeypatch,
            {"conversation": FakeConversation("device:abc")},
            sess_user="attacker-uid-9999",
        )
        out = asyncio.run(run("vision_interpret", {"image_path": "a.png"}))
        assert out is not None and "fail-closed" in out
        assert "fetched" not in calls
