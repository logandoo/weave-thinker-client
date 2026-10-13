"""device executor worker 回路（PLAN_device_executor §1.1/§2.2，主方案 §5/§7/§8/§9）。

假云端 WS 端为真实 websockets 服务（临时端口，非 mock）；worker 以真实出站
WSS（Authorization: Bearer 头）连入，tools/call 在本机真实执行（terminal/
execute_code/skill 内联脚本）。协议信封见 docs/PLAN_device_executor_impl.md
Consistency Hub。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_device_executor_service.py -v
"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import time as time_mod
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config  # noqa: E402

TRACE_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "workflows" / "device_executor.trace.log"


def _trace(line: str) -> None:
    TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TRACE_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{time_mod.strftime('%H:%M:%S')} {line}\n")


class FakeCloud:
    """假云端 /api/device/ws 端：收设备消息、主动下发请求、记录头/信封。"""

    def __init__(self, token: str = "test-token"):
        self.token = token
        self.headers = {}
        self.requests = []          # 云→设备 requests（含 id/method/params）
        self.responses = {}         # id → 响应信封
        self.notifications = []     # 设备→云 无 id 通知
        self.wire = []              # 入站帧到达序（D-91 发序断言用）
        self.connected = asyncio.Event()
        self.disconnected = asyncio.Event()
        self._ws = None
        self._next_id = 1
        self._server = None
        self.port = None

    async def _handler(self, ws):
        self._ws = ws
        self.headers = {k.lower(): v for k, v in ws.request.headers.raw_items()}
        self.connected.set()
        _trace(f"cloud: device connected auth={self.headers.get('authorization', '')[:30]}")
        try:
            async for raw in ws:
                msg = json.loads(raw)
                self.wire.append(msg)
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.responses[msg["id"]] = msg
                    _trace(f"cloud: response id={msg['id']} -> {json.dumps(msg, ensure_ascii=False)[:220]}")
                elif "method" in msg:
                    self.notifications.append(msg)
                    _trace(f"cloud: notification method={msg.get('method')}")
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.disconnected.set()
            _trace("cloud: device disconnected")

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        if self._ws is not None:
            await self._ws.close()
        self._server.close()
        await self._server.wait_closed()

    async def request(self, method: str, params: dict, timeout: float = 15.0,
                      rid=None) -> dict:
        """下发云→设备请求（W16b 信封：jsonrpc+id≡call_id，params 无 call_id）。"""
        if rid is None:
            rid = self._next_id
            self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.requests.append(payload)
        await self._ws.send(json.dumps(payload))
        _trace(f"cloud: request {json.dumps(payload, ensure_ascii=False)[:220]}")
        deadline = time_mod.monotonic() + timeout
        while time_mod.monotonic() < deadline:
            if rid in self.responses:
                return self.responses[rid]
            await asyncio.sleep(0.02)
        raise AssertionError(f"设备未应答 request id={rid} method={method}")

    async def wait_notification(self, method: str, timeout: float = 10.0) -> dict:
        deadline = time_mod.monotonic() + timeout
        while time_mod.monotonic() < deadline:
            for n in self.notifications:
                if n.get("method") == method:
                    return n
            await asyncio.sleep(0.02)
        raise AssertionError(f"未收到 {method} 通知")


def _write_binding(tmp_path: Path, cloud_port: int, token: str = "test-token") -> None:
    store_dir = tmp_path / "app"
    store_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "cloud_url": f"ws://127.0.0.1:{cloud_port}",
        "device_id": "dev-test-0001",
        "name": "test-device",
        "platform": "darwin-arm64",
        "token": token,
        "tls_verify": True,
        "created_at": "2026-10-10T00:00:00Z",
    }
    path = store_dir / "device_exec.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture()
def exec_env(tmp_path, monkeypatch):
    """LOCALSTACK_HOME 指向 tmp（真实 store 路径解析）+ 身份注入 + client_exec 覆盖。"""
    monkeypatch.setenv("LOCALSTACK_HOME", str(tmp_path))
    monkeypatch.delenv("WEAVE_LOCALSTACK_HOME", raising=False)

    ws_root = tmp_path / "workspaces" / "tester-abc12345"
    ws_root.mkdir(parents=True)
    owner = SimpleNamespace(
        id="uid-test",
        username="tester",
        # 属主已持久授权（is_permission_allowed 自动放行）——远端常规调用真实执行；
        # 权限门腿（哨兵剥离）测试内单独收紧此 dict。
        agent_permissions={"terminal_execution": True, "code_execution": True,
                           "git_execution": True, "workspace_write": True},
    )

    config = get_config()
    # terminal/code_execution 的包含性检查以 config.workspace_root 为根——
    # 测试工作区必须落在其下（否则 remote_call 下被正确拒=腿测不到执行面）。
    monkeypatch.setattr(type(config), "workspace_root", property(lambda self: ws_root.parent))
    saved = config._config.get("client_exec")
    config._config["client_exec"] = {
        "enabled": True,
        "cloud_url": "",
        "heartbeat_seconds": 1,
        "call_timeout_seconds": 120,
        "max_payload_mb": 10,
        "allowed_tools": [
            "workspace_read", "workspace_write", "workspace_edit", "workspace_glob",
            "grep", "diff", "word_count", "terminal", "process", "git",
            "execute_code", "skill_run_script", "provide_file", "provide_folder",
            "citation_render", "pdf_export",
        ],
    }

    async def _fake_identity(worker_self, binding=None):
        return owner, str(ws_root.resolve()), ws_root.name

    async def _fake_ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(ws_root.resolve()))

    from app.services.device_executor_service import DeviceExecutorWorker
    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _fake_identity)
    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _fake_ensure)

    yield SimpleNamespace(owner=owner, ws_root=ws_root, tmp_path=tmp_path)

    if saved is None:
        config._config.pop("client_exec", None)
    else:
        config._config["client_exec"] = saved


# ---------------- Slice A：握手 / terminal 回路 / 白名单镜像 ----------------

def test_initialize_handshake_bearer_header_and_caps(exec_env):
    """出站连接带 Authorization: Bearer 头（token 不进 URL）；initialize 回传能力。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                assert cloud.headers.get("authorization") == "Bearer test-token", cloud.headers
                # 云端主动 initialize → 设备应答能力协商
                resp = await cloud.request("initialize", {})
                assert "result" in resp, resp
                assert resp["result"]["ok"] is True, resp
                caps = json.loads(resp["result"]["content"])
                assert caps["protocol_version"] == 1
                assert "terminal" in caps["tools"]
                assert caps["chunking"] is True
                assert caps["max_payload_mb"] == 10
                _trace(f"test: initialize caps={caps}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_tools_call_terminal_executes_locally_and_returns_result(exec_env):
    """tools/call terminal 在设备侧真实执行并回传 result {call_id, ok, payload}。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo device-exec-ok"},
                    "timeout_seconds": 30,
                    "workspace_ctx": exec_env.ws_root.name,
                }, rid="c-term-1")
                assert "result" in resp and resp["id"] == "c-term-1", resp
                result = resp["result"]
                assert result["ok"] is True
                body = json.loads(result["content"])
                assert "_permission_needed" not in body, f"预授权属主不应触发权限门: {body}"
                assert body.get("return_code") == 0, body
                assert body.get("stdout", "").strip() == "device-exec-ok", body
                _trace(f"test: terminal roundtrip content={result['content'][:120]}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_whitelist_mirror_rejects_unknown_tool(exec_env):
    """白名单镜像 fail-closed：allowed_tools 之外的工具直接 error。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "web_search",
                    "args": {"query": "x"},
                    "timeout_seconds": 10,
                }, rid="c-bad-1")
                assert "result" in resp and resp["result"]["ok"] is False, resp
                assert "tool_not_allowed" in resp["result"]["error"], resp
                _trace("test: whitelist reject ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_whitelist_accepts_w19_tools_citation_render_and_pdf_export(exec_env):
    """W19 S1：镜像白名单含 citation_render/pdf_export 时闸门放行（不再 tool_not_allowed）。

    citation_render 走真实工具回路（无 [N] 标记亦 success:true）；pdf_export 钉闸门
    放行（不在白名单时必报 tool_not_allowed）。
    """
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "report.md").write_text("# 报告\n\n正文。\n", encoding="utf-8")

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "citation_render",
                    "args": {"file_path": "report.md", "style_id": "apa"},
                    "timeout_seconds": 30,
                }, rid="c-cit-1")
                assert "result" in resp, resp
                result = resp["result"]
                assert result["ok"] is True, result
                body = json.loads(result["content"])
                assert "tool_not_allowed" not in result["content"], result
                assert body.get("success") is True, body

                resp = await cloud.request("tools/call", {
                    "tool": "pdf_export",
                    "args": {"action": "export_file", "file_path": "report.md"},
                    "timeout_seconds": 30,
                }, rid="c-pdf-1")
                assert "result" in resp, resp
                assert "tool_not_allowed" not in str(resp["result"]), resp
                _trace("test: w19 whitelist tools gate open ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


# ---------------- Slice B：哨兵剥离/逃逸拒/内联脚本/超时公式/ctx 对齐 ----------------

def test_strip_control_keys_drops_underscore_keys():
    from app.services.device_executor_service import _strip_control_keys

    out = _strip_control_keys({"command": "echo", "_permission_granted": True, "_x": 1, "ok": 2})
    assert out == {"command": "echo", "ok": 2}


def test_effective_timeout_is_max_of_local_and_requested():
    from app.services.device_executor_service import _effective_timeout

    assert _effective_timeout(600, 120) == 600
    assert _effective_timeout(30, 120) == 120
    assert _effective_timeout(None, 120) == 120
    assert _effective_timeout("abc", 120) == 120


def test_smuggled_sentinel_stripped_and_permission_gate_runs(exec_env):
    """云端注入 `_permission_granted` 随 RPC 过境 → 门仍执行（剥离+remote_call 双保险）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        exec_env.owner.agent_permissions = {"terminal_execution": False}
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "process",
                    "args": {"action": "kill", "session_id": "x", "_permission_granted": True},
                    "timeout_seconds": 20,
                }, rid="c-perm-1")
                assert "result" in resp, resp
                payload = json.loads(resp["result"]["content"])
                assert payload.get("_permission_needed") is True, (
                    f"入站哨兵必须被剥离且权限门必须执行，实际: {payload}"
                )
                assert payload.get("_permission_key") == "terminal_execution", (
                    f"应为权限门载荷（非执行结果）: {payload}"
                )
                _trace("test: sentinel strip + gate ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_escape_rejected_despite_smuggled_sentinel_and_bypass(exec_env):
    """双哨兵注入（config.super_admin_bypass + 入站 _permission_granted）下逃逸被拒。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    config = get_config()
    prev = config.security.get("super_admin_bypass", False)
    config.security["super_admin_bypass"] = True
    try:
        async def scenario():
            async with FakeCloud() as cloud:
                _write_binding(exec_env.tmp_path, cloud.port)
                worker = DeviceExecutorWorker()
                await worker.start()
                try:
                    await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                    resp = await cloud.request("tools/call", {
                        "tool": "terminal",
                        "args": {"command": "cat /etc/hosts", "_permission_granted": True},
                        "timeout_seconds": 20,
                    }, rid="c-esc-1")
                    assert "result" in resp, resp
                    payload = json.loads(resp["result"]["content"])
                    assert "stdout" not in payload, f"逃逸命令不得产出 stdout: {payload}"
                    assert payload.get("_permission_needed") is True or payload.get("error"), (
                        f"逃逸命令必须被拒，实际: {payload}"
                    )
                    _trace("test: escape rejected ok")
                finally:
                    await worker.stop()

        asyncio.run(scenario())
    finally:
        config.security["super_admin_bypass"] = prev


def test_workspace_ctx_mismatch_or_abs_path_rejected(exec_env):
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                for bad in ("other-workspace-xyz", "/etc", "../escape"):
                    resp = await cloud.request("tools/call", {
                        "tool": "terminal",
                        "args": {"command": "echo hi"},
                        "timeout_seconds": 20,
                        "workspace_ctx": bad,
                    }, rid=f"c-ctx-{bad[:4]}")
                    assert "result" in resp and resp["result"]["ok"] is False, (bad, resp)
                    assert "workspace_mismatch" in resp["result"]["error"], (bad, resp)
                _trace("test: workspace_ctx strict alignment ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_skill_inline_script_materialized_and_executed(exec_env):
    """skill_run_script 内联脚本物化 {workspace}/skill_scripts/ 后经 _exec_skill_script 执行。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "skill_run_script",
                    "args": {"skill_name": "demo", "script_path": "hello.py", "timeout": 15},
                    "timeout_seconds": 30,
                    "script": "print('skill-inline-ok')",
                    "script_ext": ".py",
                }, rid="c-skill-1")
                assert "result" in resp, resp
                body = json.loads(resp["result"]["content"])
                assert "skill-inline-ok" in body.get("stdout", ""), body
                materialized = exec_env.ws_root / "skill_scripts" / "hello.py"
                assert materialized.is_file(), "脚本必须物化到 {workspace}/skill_scripts/"
                _trace("test: skill inline ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


# ---------------- Slice C：fs/* 分块 / cancel 杀进程 / 断线 at-most-once ----------------

async def _call_and_get(cloud, worker, call_id, tool, args, timeout_seconds=20, **extra):
    # W16b：id ≡ call_id（params 不含 call_id 字段）
    params = {"tool": tool, "args": args, "timeout_seconds": timeout_seconds}
    params.update(extra)
    return await cloud.request("tools/call", params, rid=call_id)


def test_fs_list_read_and_escape_rejected(exec_env):
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                (exec_env.ws_root / "sub").mkdir()
                (exec_env.ws_root / "sub" / "a.txt").write_text("hello-fs", encoding="utf-8")

                resp = await cloud.request("fs/list", {"path": "sub"}, rid="f1")
                assert "result" in resp, resp
                listing = resp["result"]
                assert set(listing.keys()) >= {"ok", "path", "entries", "truncated", "total"}, listing.keys()
                names = [e["name"] for e in listing["entries"]]
                assert "a.txt" in names, listing

                resp = await cloud.request("fs/read", {"path": "sub/a.txt", "offset": 0, "limit": 100}, rid="f2")
                body = resp["result"]
                assert set(body.keys()) == {"ok", "path", "offset", "limit", "total", "eof", "encoding", "content"}, body.keys()
                assert body["ok"] is True and body["content"] == "hello-fs" and body["eof"] is True, body

                for bad, code in (("../etc", "workspace_path_outside"), ("/etc", "workspace_path_invalid"),
                                  ("missing/x.txt", "workspace_path_not_found")):
                    resp = await cloud.request("fs/read", {"path": bad}, rid=f"f-bad-{code}")
                    assert resp["result"]["ok"] is False, (bad, resp)
                    assert code in resp["result"]["error"], (bad, resp)
                _trace("test: fs list/read + escape reject ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_download_chunked_with_per_chunk_and_total_digest(exec_env):
    from app.services.device_executor_service import DeviceExecutorWorker

    blob = bytes(range(256)) * 2400  # 614400 bytes ≈ 2.4 块
    (exec_env.ws_root / "blob.bin").write_bytes(blob)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp_task = asyncio.ensure_future(cloud.request("fs/download", {"path": "blob.bin"}, rid="dl1"))
                chunks = []
                deadline = time_mod.monotonic() + 15
                while time_mod.monotonic() < deadline and not resp_task.done():
                    chunks = [n for n in cloud.notifications if n.get("method") == "chunk"]
                    await asyncio.sleep(0.02)
                resp = await resp_task
                # D-91 发序：result 先至、chunk 后流——等全量块集收齐再断言
                summary = resp["result"]
                deadline = time_mod.monotonic() + 15
                while time_mod.monotonic() < deadline:
                    chunks = [n for n in cloud.notifications if n.get("method") == "chunk"]
                    if len(chunks) >= int(summary.get("chunks") or 0) and any(
                            n["params"].get("last") for n in chunks):
                        break
                    await asyncio.sleep(0.02)
                assert "result" in resp, resp
                chunks = sorted([n for n in cloud.notifications if n.get("method") == "chunk"],
                                key=lambda n: n["params"]["seq"])
                assert len(chunks) >= 3, f"600KB 应分≥3 块，实际 {len(chunks)}"
                reassembled = b""
                for i, n in enumerate(chunks):
                    p = n["params"]
                    assert set(p.keys()) == {"transfer_id", "seq", "data_b64", "sha256", "last"}, p
                    assert p["seq"] == i
                    data = base64.b64decode(p["data_b64"])
                    assert p["sha256"] == hashlib.sha256(data).hexdigest()
                    assert p["last"] is (i == len(chunks) - 1), (i, p["last"])
                    reassembled += data
                assert reassembled == blob, "重组字节必须与源文件一致"
                assert chunks[-1]["params"].get("last") is True
                summary = resp["result"]
                assert set(summary.keys()) == {"ok", "transfer_id", "sha256", "size", "chunks"}, summary
                assert summary["sha256"] == hashlib.sha256(blob).hexdigest()
                assert summary["size"] == len(blob)
                # D-91 发序：汇总先于首块
                resp_idx = next(i for i, m in enumerate(cloud.wire) if m.get("id") == resp["id"])
                first_chunk_idx = next(i for i, m in enumerate(cloud.wire)
                                       if m.get("method") == "chunk"
                                       and m["params"]["transfer_id"] == summary["transfer_id"])
                assert resp_idx < first_chunk_idx, (
                    f"D-91：汇总 result 须先于首块 chunk（result@{resp_idx} chunk@{first_chunk_idx}）")
                _trace(f"test: chunked download ok chunks={len(chunks)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


PROGRESS_LOOP = (
    "import time\n"
    "for i in range(40):\n"
    "    time.sleep(0.5)\n"
    "    open('progress.txt', 'a').write('x')\n"
)


def test_tools_cancel_terminates_running_command(exec_env):
    """tools/cancel → asyncio 任务取消 + 子进程终止（进度文件停止增长）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                call = asyncio.ensure_future(_call_and_get(
                    cloud, worker, "c-sleep-1", "terminal",
                    {"command": "for i in $(seq 1 40); do echo x >> progress.txt; sleep 0.5; done"},
                    timeout_seconds=30,
                ))
                progress = exec_env.ws_root / "progress.txt"
                deadline = time_mod.monotonic() + 10
                while time_mod.monotonic() < deadline and not progress.exists():
                    await asyncio.sleep(0.05)
                assert progress.exists(), "长命令应开始产出进度"
                resp = await cloud.request("tools/cancel", {"call_id": "c-sleep-1"}, rid="cancel-1")
                assert "result" in resp, resp
                call_resp = await asyncio.wait_for(call, timeout=5)
                assert call_resp["result"]["ok"] is False and "cancelled" in call_resp["result"]["error"], call_resp
                # 子进程死亡证明：进度文件冻结（泄漏则每 0.5s 增长）
                await asyncio.sleep(0.3)
                size1 = progress.stat().st_size
                await asyncio.sleep(2.0)
                size2 = progress.stat().st_size
                assert size1 == size2, f"cancel 后子进程仍在写（{size1}->{size2}）——进程未被终止"
                _trace(f"test: cancel kills command ok ({size1}B frozen)")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_execute_code_service_cancel_kills_subprocess(exec_env, monkeypatch):
    """execute_code→子进程终止：服务在 CancelledError 时杀子进程（PLAN §1.1 映射）。"""
    from app.services.code_execution_service import CodeExecutionService

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace",
                        lambda *a, **k: __import__("asyncio").sleep(0, result=SimpleNamespace(root_path=str(exec_env.ws_root))))
    marker = exec_env.ws_root / "progress.txt"

    async def scenario():
        svc = CodeExecutionService()
        task = asyncio.ensure_future(svc.execute_python(PROGRESS_LOOP, cwd=str(exec_env.ws_root)))
        deadline = time_mod.monotonic() + 10
        while time_mod.monotonic() < deadline and not marker.exists():
            await asyncio.sleep(0.05)
        assert marker.exists(), "脚本应开始产出进度"
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await asyncio.sleep(0.3)
        size1 = marker.stat().st_size
        await asyncio.sleep(2.0)
        size2 = marker.stat().st_size
        assert size1 == size2, f"cancel 后子进程仍在写（{size1}->{size2}）——进程未被终止"

    asyncio.run(scenario())


def test_disconnect_at_most_once_and_reconnect_recovers(exec_env):
    """断线：在途调用 failed（at-most-once 不重放）；重连后心跳恢复、新调用可用。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                orphan_id = 99
                cloud._next_id = 100
                await cloud._ws.send(json.dumps({
                    "jsonrpc": "2.0", "id": orphan_id, "method": "tools/call",
                    "params": {"tool": "terminal",
                               "args": {"command": "sleep 20"}, "timeout_seconds": 30},
                }))
                await asyncio.sleep(0.3)
                await cloud._ws.close()  # 模拟断线（在途调用未完成）
                await asyncio.wait_for(cloud.disconnected.wait(), timeout=5)

                cloud.connected.clear()
                cloud.disconnected.clear()
                await asyncio.wait_for(cloud.connected.wait(), timeout=15)  # 退避重连
                await cloud.wait_notification("heartbeat", timeout=10)  # 心跳恢复
                resp = await _call_and_get(cloud, worker, "c-after-1", "terminal",
                                           {"command": "echo after-reconnect"})
                assert "result" in resp, resp
                assert resp["result"]["ok"] is True, resp
                body = json.loads(resp["result"]["content"])
                assert body.get("stdout", "").strip() == "after-reconnect", body
                assert orphan_id not in cloud.responses, (
                    "at-most-once：断线在途调用不得重放应答"
                )
                _trace("test: disconnect at-most-once + reconnect ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_timeout_reply_and_process_kill(exec_env):
    """等待上限=max(本地默认, 请求值) 到点回 error timeout，且子进程被终止。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    config = get_config()
    config._config["client_exec"]["call_timeout_seconds"] = 5  # 属性下限钳制 5s

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                progress = exec_env.ws_root / "timeout_progress.txt"
                cmd = f"for i in $(seq 1 30); do echo x >> {progress.name}; sleep 0.5; done"
                t0 = time_mod.monotonic()
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": cmd}, "timeout_seconds": 1,
                }, rid="c-to-1")
                elapsed = time_mod.monotonic() - t0
                assert resp["result"]["ok"] is False and "timeout" in resp["result"]["error"], resp
                assert elapsed < 8, f"应按上限截断，实际 {elapsed:.1f}s"
                await asyncio.sleep(0.3)
                if progress.exists():
                    size1 = progress.stat().st_size
                    await asyncio.sleep(1.5)
                    assert progress.stat().st_size == size1, "超时后子进程仍在写——未被终止"
                _trace(f"test: timeout reply ok ({elapsed:.1f}s)")
            finally:
                await worker.stop()

    asyncio.run(scenario())


# ---------------- 评审修复实证腿（A4.9 双评审 Critical/Important）----------------

def test_fs_symlink_escape_rejected(exec_env):
    """越根符号链接拒绝（判据 #7 明列形态；评审 B 腿补）。"""
    outside = exec_env.tmp_path / "outside-secret.txt"
    outside.write_text("SECRET", encoding="utf-8")
    (exec_env.ws_root / "leak.txt").symlink_to(outside)

    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("fs/read", {"path": "leak.txt"}, rid="sym1")
                assert resp["result"]["ok"] is False, resp
                assert "workspace_path_outside" in resp["result"]["error"], resp
                _trace("test: symlink escape rejected ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_download_empty_file_single_shot(exec_env):
    """空文件走 data_b64 单包（W16b：≤CHUNK_SIZE 单包；sha256=整体摘要）。"""
    (exec_env.ws_root / "empty.bin").write_bytes(b"")
    empty_digest = hashlib.sha256(b"").hexdigest()

    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("fs/download", {"path": "empty.bin"}, rid="dl0")
                result = resp["result"]
                assert set(result.keys()) == {"ok", "data_b64", "sha256"}, result
                assert result["ok"] is True and result["data_b64"] == "", result
                assert result["sha256"] == empty_digest, result
                chunks = [n for n in cloud.notifications if n.get("method") == "chunk"]
                assert chunks == [], "单包路径不得发 chunk 通知"
                _trace("test: empty-file single-shot ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_cancel_kills_process_session_via_owner_id(exec_env):
    """tools/cancel → process 会话 kill 用属主 id（评审 A#3/B#3：args.user_id 并不存在）。"""
    from app.services.device_executor_service import DeviceExecutorWorker
    from app.services.process_session_service import process_manager

    async def scenario():
        started = await process_manager.start(
            user_id=exec_env.owner.id,
            command="sleep 30", cwd=str(exec_env.ws_root), workspace_root=str(exec_env.ws_root),
        )
        assert started.get("ok"), started
        session_id = started["session_id"]

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                pending = asyncio.ensure_future(asyncio.sleep(30))
                worker._inflight["c-proc-1"] = {
                    "id": 77, "task": pending, "tool": "process",
                    "args": {"action": "start", "session_id": session_id},
                    "owner_id": exec_env.owner.id, "replied": False,
                }
                resp = await cloud.request("tools/cancel", {"call_id": "c-proc-1"}, rid="cancel-2")
                assert "result" in resp, resp
                await asyncio.sleep(0.5)
                check = await process_manager.read(
                    user_id=exec_env.owner.id, session_id=session_id, wait_ms=0)
                assert check.get("running") is False and check.get("exit_code") is not None, (
                    f"会话应已被 kill（sleep 30 不得自行退出），实际: {check}"
                )
                _trace("test: cancel kills process session ok")
            finally:
                pending.cancel()
                await worker.stop()

    asyncio.run(scenario())


def test_reset_severs_connection_and_repair_reconnects(exec_env):
    """Critical 修复实证：reset=断连+拒新调用；重配对后可重连（_auth_failed 闩不得一闩到底）。"""
    from app.services import device_pairing_service as pairing
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port, token="fake-test-token-0000")
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                # 1) reset：store 清除 + wake → 连接必须被踢断
                pairing.reset_binding()
                worker.wake()
                await asyncio.wait_for(cloud.disconnected.wait(), timeout=8)
                _trace("test: reset severed connection ok")
                # 2) 拒新调用腿：仍带旧凭据的连接即使存活也拒绝（_binding_alive）
                assert worker._binding_alive() is False
                # 3) 重配对（新 token）→ wake → 必须重连（auth 闩已清）
                cloud.connected.clear()
                cloud.disconnected.clear()
                _write_binding(exec_env.tmp_path, cloud.port, token="fake-test-token-1111")
                worker.wake()
                await asyncio.wait_for(cloud.connected.wait(), timeout=15)
                resp = await _call_and_get(cloud, worker, "c-repair-1", "terminal",
                                           {"command": "echo reconnected"})
                body = json.loads(resp["result"]["content"])
                assert body.get("stdout", "").strip() == "reconnected", body
                _trace("test: re-pair reconnect after reset ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_tasks_cancelled_on_stop(exec_env):
    """NEW-1：fs/* 任务随 worker 停止取消（不得跨连接/跨生命周期外泄字节）。

    复评 I-4 腿：send 侧阻塞保证在途（不靠文件大小/定时），stop 前快照、
    stop 后断言 done+cancelled（不以已清空的登记表做空断言）。
    """
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "big.bin").write_bytes(b"x" * 4096)

    started = asyncio.Event()
    release = asyncio.Event()

    async def scenario():
        worker = DeviceExecutorWorker()
        real_send = worker._send_chunks

        async def slow_send(ws, req_id, call_id, target):
            started.set()
            await release.wait()
            return await real_send(ws, req_id, call_id, target)

        worker._send_chunks = slow_send
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                task = asyncio.ensure_future(
                    cloud.request("fs/download", {"path": "big.bin"}, rid="dl-cancel"))
                await asyncio.wait_for(started.wait(), timeout=10)
                inflight = list(worker._fs_tasks.values())
                assert inflight and not all(t.done() for t in inflight), "应捕获在途 fs 任务"
                await worker.stop()  # 阻塞点在事件 await（非线程）→取消即刻生效
                deadline = time_mod.monotonic() + 10
                while time_mod.monotonic() < deadline and not all(t.done() for t in inflight):
                    await asyncio.sleep(0.02)
                assert all(t.done() for t in inflight), "stop() 必须收束全部 fs 任务"
                assert all(t.cancelled() for t in inflight), "收束=取消（不得回字节）"
                assert "dl-cancel" not in cloud.responses, "被取消的传输不得回包"
                task.cancel()
                _trace("test: fs tasks cancelled on stop ok")
            finally:
                release.set()
                await worker.stop()

    asyncio.run(scenario())


# ---------------- W19 三 RPC（fs/zip · fs/office-html · fs/office-pdf，S2）----------------

REAL_SOFFICE = __import__("shutil").which("soffice") or __import__("shutil").which("libreoffice")


async def _decode_result_bytes(resp: dict, cloud: "FakeCloud") -> bytes:
    """fs/* result 同形解析：单包 data_b64 或 transfer_id+chunk 流重组（带逐块校验）。

    D-91 发序下 chunk 流在 result 之后到达——先等全量块集收齐（有界），
    再断言「汇总先于首块」与逐块/整包摘要。
    """
    result = resp["result"]
    assert result.get("ok") is True, result
    data_b64 = result.get("data_b64")
    if isinstance(data_b64, str):
        assert set(result.keys()) == {"ok", "data_b64", "sha256"}, result.keys()
        raw = base64.b64decode(data_b64)
        assert result["sha256"] == hashlib.sha256(raw).hexdigest()
        return raw
    assert set(result.keys()) == {"ok", "transfer_id", "sha256", "size", "chunks"}, result.keys()
    expected_n = int(result["chunks"])
    deadline = time_mod.monotonic() + 15
    chunks: list = []
    while time_mod.monotonic() < deadline:
        chunks = [n for n in cloud.notifications if n.get("method") == "chunk"
                  and n["params"]["transfer_id"] == result["transfer_id"]]
        if len(chunks) >= expected_n and any(n["params"].get("last") for n in chunks):
            break
        await asyncio.sleep(0.02)
    chunks = sorted(chunks, key=lambda n: n["params"]["seq"])
    assert len(chunks) == expected_n, f"应收 {expected_n} 块，实际 {len(chunks)}"
    # D-91 发序：汇总 result 必须先于首块 chunk（上传走云端汇编窗，不再挤 RPC 等待窗）
    resp_idx = next(i for i, m in enumerate(cloud.wire) if m.get("id") == resp["id"])
    first_chunk_idx = next(i for i, m in enumerate(cloud.wire)
                           if m.get("method") == "chunk"
                           and m["params"]["transfer_id"] == result["transfer_id"])
    assert resp_idx < first_chunk_idx, (
        f"D-91：汇总 result 须先于首块 chunk（result@{resp_idx} chunk@{first_chunk_idx}）")
    raw = b""
    for i, n in enumerate(chunks):
        p = n["params"]
        assert set(p.keys()) == {"transfer_id", "seq", "data_b64", "sha256", "last"}, p
        assert p["seq"] == i
        piece = base64.b64decode(p["data_b64"])
        assert p["sha256"] == hashlib.sha256(piece).hexdigest()
        assert p["last"] is (i == len(chunks) - 1), (i, p["last"])
        raw += piece
    assert hashlib.sha256(raw).hexdigest() == result["sha256"]
    assert result["size"] == len(raw) and result["chunks"] == len(chunks)
    return raw


def test_fs_zip_single_packet_roundtrip(exec_env):
    """fs/zip：小目录 → 单包 result 与 fs/download 同形，zip 成员逐字节一致。"""
    import io
    import zipfile

    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "pack"
    (pack / "sub").mkdir(parents=True)
    (pack / "a.txt").write_text("alpha-内容", encoding="utf-8")
    (pack / "sub" / "b.txt").write_text("beta", encoding="utf-8")

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("fs/zip", {"path": "pack"}, rid="zip1")
                raw = await _decode_result_bytes(resp, cloud)
                zf = zipfile.ZipFile(io.BytesIO(raw))
                names = sorted(zf.namelist())
                assert names == ["pack/a.txt", "pack/sub/b.txt"], names
                assert zf.read("pack/a.txt").decode("utf-8") == "alpha-内容"
                assert zf.read("pack/sub/b.txt").decode("utf-8") == "beta"
                _trace(f"test: fs/zip single packet ok bytes={len(raw)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_zip_chunked_with_per_chunk_digest(exec_env):
    """fs/zip：大目录（不可压 600KB）→ 分块汇总恰 {ok,transfer_id,sha256,size,chunks}+重组一致。"""
    import io
    import zipfile

    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "bigpack"
    pack.mkdir()
    blob = os.urandom(600 * 1024)
    (pack / "rand.bin").write_bytes(blob)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "bigpack"}, rid="zip2"))
                raw = await _decode_result_bytes(resp, cloud)
                zf = zipfile.ZipFile(io.BytesIO(raw))
                assert zf.read("bigpack/rand.bin") == blob, "zip 成员字节必须与源一致"
                assert len(raw) > 256 * 1024, "600KB 不可压载荷应走分块路径"
                _trace(f"test: fs/zip chunked ok bytes={len(raw)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_fs_office_pdf_real_soffice(exec_env):
    """fs/office-pdf：真实 docx → PDF 字节（%PDF- 魔数）回传，result 与 fs/download 同形。"""
    from docx import Document

    from app.services.device_executor_service import DeviceExecutorWorker

    doc = Document()
    doc.add_heading("W19 预览", level=1)
    doc.add_paragraph("device office pdf")
    doc.save(str(exec_env.ws_root / "doc.docx"))

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await asyncio.ensure_future(
                    cloud.request("fs/office-pdf", {"path": "doc.docx"}, rid="opdf1"))
                raw = await _decode_result_bytes(resp, cloud)
                assert raw[:5] == b"%PDF-", raw[:20]
                _trace(f"test: fs/office-pdf ok bytes={len(raw)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_fs_office_html_real_soffice(exec_env):
    """fs/office-html：真实 xlsx → 自包含 HTML（表格标记）回传。"""
    from openpyxl import Workbook

    from app.services.device_executor_service import DeviceExecutorWorker

    wb = Workbook()
    ws = wb.active
    ws.append(["名称", "数值"])
    ws.append(["w19", 42])
    wb.save(str(exec_env.ws_root / "sheet.xlsx"))

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await asyncio.ensure_future(
                    cloud.request("fs/office-html", {"path": "sheet.xlsx"}, rid="ohtml1"))
                raw = await _decode_result_bytes(resp, cloud)
                text = raw.decode("utf-8", errors="replace")
                assert "<table" in text.lower(), text[:200]
                assert "w19" in text
                _trace(f"test: fs/office-html ok bytes={len(raw)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not REAL_SOFFICE, reason="soffice not installed")
def test_fs_office_html_chunked_large_workbook(exec_env):
    """fs/office-html 大工作表 → HTML 超 CHUNK_SIZE 走分块汇总（result 同形分块腿）。"""
    from openpyxl import Workbook

    from app.services.device_executor_service import DeviceExecutorWorker

    wb = Workbook()
    ws = wb.active
    for i in range(800):
        ws.append([f"row-{i:04d}-padpadpadpadpadpadpadpadpadpad", i, i * 2, i * 3, i * 4, i * 5, i * 6, i * 7])
    wb.save(str(exec_env.ws_root / "wide.xlsx"))

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await asyncio.ensure_future(
                    cloud.request("fs/office-html", {"path": "wide.xlsx"}, rid="ohtml2"))
                raw = await _decode_result_bytes(resp, cloud)
                assert len(raw) > 256 * 1024, f"大表 HTML 应超单包阈值，实际 {len(raw)}"
                assert b"<table" in raw.lower()
                _trace(f"test: fs/office-html chunked ok bytes={len(raw)}")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_w19_error_surface(exec_env):
    """三新方法错误面（判据 #4）：类型/形状/存在性/逃逸/预览禁用各有确定错误串。"""
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "plain.txt").write_text("x", encoding="utf-8")
    (exec_env.ws_root / "wrong.docx").write_bytes(b"junk")
    (exec_env.ws_root / "wrong.xlsx").write_bytes(b"junk")

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)

                async def err_of(method, params, rid):
                    resp = await cloud.request(method, params, rid=rid)
                    assert "result" in resp, resp
                    body = resp["result"]
                    assert body["ok"] is False, body
                    return body["error"]

                e = await err_of("fs/zip", {"path": "plain.txt"}, "e1")
                assert e == "workspace_path_invalid: path is not a directory", e
                e = await err_of("fs/zip", {"path": "missing"}, "e2")
                assert e == "workspace_path_not_found: directory not found in workspace", e
                e = await err_of("fs/zip", {"path": "../outside"}, "e3")
                assert "workspace_path_outside" in e or "workspace_path_invalid" in e, e
                e = await err_of("fs/office-pdf", {"path": "plain.txt"}, "e4")
                assert "office_preview_failed" in e and "unsupported" in e, e
                e = await err_of("fs/office-html", {"path": "wrong.docx"}, "e5")
                assert "office_preview_failed" in e and "spreadsheet" in e, e
                e = await err_of("fs/office-pdf", {"path": "missing.docx"}, "e6")
                assert "workspace_path_not_found" in e, e

                cfg = get_config()
                saved = cfg._config.get("office_preview", {})
                cfg._config["office_preview"] = {**saved, "enabled": False}
                try:
                    e = await err_of("fs/office-pdf", {"path": "wrong.docx"}, "e7")
                    assert "office_preview_unavailable" in e, e
                    e = await err_of("fs/office-html", {"path": "wrong.xlsx"}, "e8")
                    assert "office_preview_unavailable" in e, e
                finally:
                    cfg._config["office_preview"] = saved
                _trace("test: fs w19 error surface ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_w19_binding_gate_rejects_when_unbound(exec_env):
    """三新方法同享 _binding_alive 门（判据 #5）：凭据换绑后旧连接拒新调用。

    踢断面（token 比对周期 kick+重连）由 test_reset_severs_connection_and_repair_reconnects
    覆盖；本腿 stub 掉 _kick 专测「每调用门」的应答面（换 token 本会被周期检查踢断并以
    新凭据重连，at-most-once 下请求应答已随连接丢弃）。
    """
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "gate"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            worker._kick = lambda reason: None  # 本腿不测踢断面
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                # 先握手一来回越过「建连即复核凭据」窗口（connected 事件早于 worker
                # 复核完成——此刻换 token 会被 _safe_close+以新凭据重连吞掉请求）
                warm = await cloud.request("fs/list", {"path": "gate"}, rid="gate-warm")
                assert warm["result"]["ok"] is True, warm
                assert worker._binding_alive() is True
                _write_binding(exec_env.tmp_path, cloud.port,
                               token=f"rotated-{os.urandom(8).hex()}")
                assert worker._binding_alive() is False
                for i, (method, params) in enumerate([
                    ("fs/zip", {"path": "gate"}),
                    ("fs/office-pdf", {"path": "a.docx"}),
                    ("fs/office-html", {"path": "a.xlsx"}),
                ]):
                    resp = await cloud.request(method, params, rid=f"gate-ur-{i}")
                    assert resp["result"]["ok"] is False, resp
                    assert "unauthorized" in resp["result"]["error"], resp
                _trace("test: fs w19 binding gate reject ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_zip_cancel_drains_build_and_unlinks_tmp(exec_env):
    """fs/zip 生命周期（判据 #5）：在途入 _fs_tasks、随 stop 收束、取消路径排空后台
    打包线程后删临时 zip（不得留 wt_ws_zip_* 孤儿）。"""
    import tempfile
    import threading

    import app.services.workspace_files_service as wfs
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "drain"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    before = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*"))

    async def scenario():
        real_zip = wfs.build_folder_zip

        def slow_zip(root, target, dest):
            started.set()
            if not release.wait(timeout=15):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                task = asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "drain"}, rid="drain1"))
                await asyncio.to_thread(started.wait, 10)
                inflight = list(worker._fs_tasks.values())
                assert inflight, "fs/zip 在途任务必须登记 _fs_tasks"
                assert not all(t.done() for t in inflight), "在途任务不应已完成"
                assert "drain1" not in cloud.responses, "未完成不得回包"
                # 限时放行后台打包线程（复评 G-4 去 flaky：不让线程 15s 超时去赌 drain 窗口）
                asyncio.get_event_loop().call_later(0.3, release.set)
                await worker.stop()
                # 快照断言（评审 B#2：stop() 清空登记表后 all([]) 恒真=空断言）
                deadline = time_mod.monotonic() + 10
                while time_mod.monotonic() < deadline and not all(t.done() for t in inflight):
                    await asyncio.sleep(0.05)
                assert all(t.done() for t in inflight), "stop() 必须收束 fs/zip 任务"
                assert all(t.cancelled() for t in inflight), "收束=取消（不得继续跑）"
                task.cancel()
            finally:
                wfs.build_folder_zip = real_zip
                release.set()  # 让后台打包线程收尾 → 取消路径的 drain+unlink 走完
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline:
                    if not (set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*")) - before):
                        break
                    await asyncio.sleep(0.05)
                await worker.stop()

        leftovers = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*")) - before
        assert not leftovers, f"取消路径必须清理临时 zip，残留: {leftovers}"
        _trace("test: fs/zip cancel drains build and unlinks tmp ok")

    asyncio.run(scenario())


# ---------------- FCV 补腿（fresh-context 复核指出的判据 #1/#9 子句）----------------

def test_disabled_config_does_not_start_worker(exec_env):
    """enabled=false → worker 不启动（判据 #1 行为腿）。"""
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    get_config()._config["client_exec"]["enabled"] = False

    async def scenario():
        worker = DeviceExecutorWorker()
        await worker.start()
        assert worker._task is None, "enabled=false 不得启动 worker 循环"
        st = worker.status()
        assert st["enabled"] is False and st["connected"] is False
        await worker.stop()

    asyncio.run(scenario())


def test_backoff_doubles_with_cap(exec_env):
    """指数退避 1→2→4…cap60（判据 #1 子腿：以 spy 钉序列，不做脆弱计时断言）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    delays = []

    async def scenario():
        worker = DeviceExecutorWorker()

        async def fake_idle(seconds):
            delays.append(seconds)

        worker._idle_wait = fake_idle  # spy：只测退避序列生成
        # 连接必然失败的凭据（关端口）
        (exec_env.tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (exec_env.tmp_path / "app" / "device_exec.json").write_text(
            json.dumps({"cloud_url": "ws://127.0.0.1:1", "token": "t", "device_id": "d"}), encoding="utf-8")
        worker._stopped = False
        task = asyncio.ensure_future(worker._run())
        await asyncio.sleep(1.2)
        worker._stopped = True
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        assert delays[:3] == [1.0, 2.0, 4.0], f"退避序列应 1,2,4…，实际 {delays[:5]}"

    asyncio.run(scenario())


def test_ws_url_scheme_mapping_and_token_not_in_url(exec_env):
    """http(s)→ws(s) 映射；token 只进 Authorization 头不进 URL（判据 #1/#9 子腿）。"""
    from app.services.device_executor_service import _ws_url

    assert _ws_url("http://host:8158") == "ws://host:8158/api/device/ws"
    assert _ws_url("https://host:8158") == "wss://host:8158/api/device/ws"
    assert _ws_url("ws://127.0.0.1:9000") == "ws://127.0.0.1:9000/api/device/ws"
    assert _ws_url("wss://host") == "wss://host/api/device/ws"
    url = _ws_url("https://host:8158")
    assert "token" not in url and "fake-test-token" not in url


def test_wire_envelope_matches_upstream_contract(exec_env):
    """契约钉（W16b Hub）：信封带 jsonrpc；tools/call result 恰 {ok,content}；
    chunk 恰 {transfer_id,seq,data_b64,sha256,last}；id≡call_id。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal", "args": {"command": "echo x"}, "timeout_seconds": 10,
                }, rid="contract-1")
                assert resp.get("jsonrpc") == "2.0", resp
                assert resp["id"] == "contract-1", "id ≡ call_id（应答原样回 id）"
                assert set(resp["result"].keys()) == {"ok", "content"}, resp["result"]

                (exec_env.ws_root / "mid.bin").write_bytes(b"y" * (300 * 1024))
                t = asyncio.ensure_future(cloud.request("fs/download", {"path": "mid.bin"}, rid="contract-2"))
                deadline = time_mod.monotonic() + 15
                while time_mod.monotonic() < deadline and not t.done():
                    await asyncio.sleep(0.02)
                resp2 = await t
                result = resp2["result"]
                assert set(result.keys()) >= {"ok", "transfer_id", "sha256"}, result
                chunks = [n for n in cloud.notifications if n.get("method") == "chunk"]
                assert chunks, "分块路径必须发 chunk"
                for c in chunks:
                    cp = c["params"]
                    assert set(cp.keys()) == {"transfer_id", "seq", "data_b64", "sha256", "last"}, cp
                    assert cp["transfer_id"] == result["transfer_id"]
                    assert c.get("jsonrpc") == "2.0", "chunk 通知必须带 jsonrpc（评审 C1 同族）"
                assert chunks[-1]["params"].get("last") is True

                hb = await cloud.wait_notification("heartbeat", timeout=15)
                assert hb.get("jsonrpc") == "2.0", f"heartbeat 通知必须带 jsonrpc: {hb}"
                assert set(hb["params"].keys()) == {"device_id", "ts"}, hb["params"]
                _trace("test: wire envelope contract ok (incl. heartbeat jsonrpc)")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_protocol_error_envelope_and_failure_shape(exec_env):
    """判据 #3 补钉：败路 result 恰 {ok,error}；协议级失败走 {id, error:{code,message}}；
    tools/cancel 未知 call_id → error.call_not_found（信封 call_id=请求 id）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("no/such/method", {}, rid="proto-1")
                assert resp.get("jsonrpc") == "2.0" and resp["id"] == "proto-1", resp
                assert set(resp["error"].keys()) >= {"code", "message"}, resp["error"]
                assert resp["error"]["code"] == "method_not_found", resp

                resp2 = await cloud.request("tools/call", {
                    "tool": "web_search", "args": {}, "timeout_seconds": 5,
                }, rid="proto-2")
                assert set(resp2["result"].keys()) == {"ok", "error"}, resp2["result"]
                assert resp2["result"]["ok"] is False

                resp3 = await cloud.request("tools/cancel", {"call_id": "ghost"}, rid="proto-3")
                assert resp3["error"]["code"] == "call_not_found", resp3
                _trace("test: protocol error envelope ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_fs_w19_office_error_mapping_coded(exec_env):
    """评审 B#3/B#4：office 转换异常→错误串必须带契约码（internal_error/office_preview_*），
    且 PreparationError 不回显 OS 路径。monkeypatch 共用转换函数=驱动映射腿。"""
    import app.services.office_preview_service as ops
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "doc.docx").write_bytes(b"x")
    (exec_env.ws_root / "sheet.xlsx").write_bytes(b"x")

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                real = ops.convert_workspace_document

                async def send_expect(rid, method, path, exc, needle):
                    async def raiser(src, **kw):
                        raise exc
                    ops.convert_workspace_document = raiser
                    try:
                        resp = await cloud.request(method, {"path": path}, rid=rid)
                    finally:
                        ops.convert_workspace_document = real
                    body = resp["result"]
                    assert body["ok"] is False, body
                    assert needle in body["error"], (needle, body)
                    assert "/" not in body["error"], f"错误串不得回显路径: {body}"
                    return body["error"]

                e = await send_expect("m1", "fs/office-pdf", "doc.docx",
                                      ops.OfficePreviewUnavailable("LibreOffice is not installed"),
                                      "office_preview_unavailable:")
                e = await send_expect("m2", "fs/office-html", "sheet.xlsx",
                                      ops.OfficePreviewFailed("soffice exited 3"),
                                      "office_preview_failed:")
                e = await send_expect("m3", "fs/office-pdf", "doc.docx",
                                      ops.OfficePreviewPreparationError("preview preparation failed"),
                                      "internal_error: preview preparation failed")
                assert "Errno" not in e and "/var/" not in e and "cache" not in e, e
                _trace("test: fs w19 office error mapping coded ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_tools_cancel_aborts_inflight_fs_zip(exec_env):
    """W19 生命周期（评审 I-3）：tools/cancel {call_id=请求 id} 中止在途 fs/zip
    （打包线程被排空收尾、不回字节）。"""
    import tempfile
    import threading

    import app.services.workspace_files_service as wfs
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "abort"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    before = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*"))

    async def scenario():
        real_zip = wfs.build_folder_zip

        def slow_zip(root, target, dest):
            started.set()
            if not release.wait(timeout=15):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                task = asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "abort"}, rid="abort1"))
                await asyncio.to_thread(started.wait, 10)
                assert worker._fs_tasks.get("abort1") is not None, "fs 任务须按请求 id 登记"
                resp = await cloud.request("tools/cancel", {"call_id": "abort1"}, rid="ac1")
                assert resp["result"] == {"cancelled": True}, resp
                release.set()
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline and not task.done():
                    await asyncio.sleep(0.05)
                assert "abort1" not in cloud.responses, "被取消的 fs/zip 不得回字节结果"
                leftovers = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*")) - before
                assert not leftovers, f"取消后临时 zip 必须清零: {leftovers}"
                _trace("test: tools/cancel aborts in-flight fs/zip ok")
            finally:
                wfs.build_folder_zip = real_zip
                release.set()
                task.cancel()
                await worker.stop()

    asyncio.run(scenario())


def test_fs_heavy_inflight_cap_rejects_beyond_limit(exec_env):
    """D-92：fs 重活（zip/office-*）并发上限，超限回 ok:false + too_many_in_flight；
    轻量 fs/*（read）不受该上限影响（上限只闸重活）。"""
    import tempfile
    import threading

    import app.services.workspace_files_service as wfs
    from app.services.device_executor_service import DeviceExecutorWorker

    cap_dir = exec_env.ws_root / "cap"
    cap_dir.mkdir()
    (cap_dir / "a.txt").write_text("x", encoding="utf-8")
    (exec_env.ws_root / "light.txt").write_text("light", encoding="utf-8")

    started = threading.Event()  # 至少一个打包线程已进入（其余排队即计）
    release = threading.Event()
    entered = []

    async def scenario():
        worker = DeviceExecutorWorker()
        cap_n = worker._max_heavy_fs
        real_zip = wfs.build_folder_zip

        def slow_zip(root, target, dest):
            entered.append(target)
            started.set()
            if not release.wait(timeout=15):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                inflight = [asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "cap"}, rid=f"cap-{i}"))
                    for i in range(cap_n)]
                await asyncio.to_thread(started.wait, 10)
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline and len(entered) < cap_n:
                    await asyncio.sleep(0.02)
                assert len(entered) >= cap_n, f"应有 {cap_n} 个重活进入执行"

                # 超限一个：必须立即收到 too_many_in_flight（而非排队/挂起）
                resp = await cloud.request("fs/zip", {"path": "cap"}, rid="cap-over", timeout=5)
                assert resp["result"]["ok"] is False, resp
                assert "too_many_in_flight" in resp["result"]["error"], resp

                # 轻量 fs/* 不受重活上限影响：满载时 fs/read 仍即时可用
                resp = await cloud.request("fs/read", {"path": "light.txt", "offset": 0, "limit": 10},
                                           rid="cap-light", timeout=5)
                assert resp["result"]["ok"] is True, resp
                assert resp["result"]["content"] == "light", resp

                release.set()
                done, pending = await asyncio.wait(inflight, timeout=10)
                assert not pending, "放行后在途重活应收敛"
                for t in done:
                    assert t.result()["result"]["ok"] is True, t.result()
                # 上限槽位必须归还（复评 G-1：删 finally 归还=前 4 个后永久拒）
                assert worker._heavy_fs_inflight == 0, "重活槽位必须归还"
                resp = await cloud.request("fs/zip", {"path": "cap"}, rid="cap-after", timeout=10)
                assert resp["result"]["ok"] is True, resp
                _trace(f"test: fs heavy inflight cap {cap_n} ok")
            finally:
                wfs.build_folder_zip = real_zip
                release.set()
                await worker.stop()

    asyncio.run(scenario())


# ---------------- W19 补波 2：生产侧加固（zip 限时 · oversize 预检 · progress）----------------

def test_fs_zip_build_timeout_replies_timeout(exec_env):
    """打包超过生产预算（`call_max_extension_seconds`，补波 3 起）→ ok:false + timeout 码
    （不再干等云端 504）；排空后临时件清零。"""
    import tempfile
    import threading

    import app.services.workspace_files_service as wfs
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "slowpack"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    before = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*"))

    async def scenario():
        real_zip = wfs.build_folder_zip

        def slow_zip(root, target, dest):
            started.set()
            if not release.wait(timeout=20):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        cfg = get_config()
        saved_budget = cfg._config["client_exec"].get("call_max_extension_seconds")
        cfg._config["client_exec"]["call_max_extension_seconds"] = 5  # 生产预算（补波 3 起用此键）
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                resp = await cloud.request("fs/zip", {"path": "slowpack"}, rid="to1", timeout=15)
                body = resp["result"]
                assert body["ok"] is False, body
                assert body["error"].startswith("timeout: fs/zip build exceeded"), body
                assert started.is_set(), "打包应已进入"
                replies = [m for m in cloud.wire if m.get("id") == "to1"]
                assert len(replies) == 1, f"同一请求恰一应答（防双发）: {len(replies)}"
                release.set()
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline:
                    if not (set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*")) - before):
                        break
                    await asyncio.sleep(0.05)
                leftovers = set(Path(tempfile.gettempdir()).glob("wt_ws_zip_*")) - before
                assert not leftovers, f"超时后临时 zip 必须清零: {leftovers}"
                _trace("test: fs/zip build timeout replies timeout ok")
            finally:
                wfs.build_folder_zip = real_zip
                release.set()
                cfg._config["client_exec"]["call_max_extension_seconds"] = saved_budget
                await worker.stop()

    asyncio.run(scenario())


def test_fs_byte_channel_rejects_oversize_before_stream(exec_env):
    """oversize 预检：产物超 max_payload_mb 在发字节前拒（payload_too_large），
    三面同检（fs/download 与 fs/zip 共用 _send_chunks 出口）；不得有任何 chunk 流。"""
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    big = os.urandom(2 * 1024 * 1024)  # 2MB 不可压
    (exec_env.ws_root / "big.bin").write_bytes(big)
    pack = exec_env.ws_root / "bigpack2"
    pack.mkdir()
    (pack / "rand.bin").write_bytes(big)

    async def scenario():
        cfg = get_config()
        saved = cfg._config["client_exec"]["max_payload_mb"]
        cfg._config["client_exec"]["max_payload_mb"] = 1
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                for rid, method, params in (("ov1", "fs/download", {"path": "big.bin"}),
                                            ("ov2", "fs/zip", {"path": "bigpack2"})):
                    resp = await cloud.request(method, params, rid=rid)
                    body = resp["result"]
                    assert body["ok"] is False, body
                    assert body["error"].startswith("payload_too_large"), body
                    # 上游 remote_dispatch_router 以「超过」嗅探 oversize 分类（评审 I-1）——
                    # 该词必须在消息里，否则云端文案面退化
                    assert "超过" in body["error"], body
                    assert "transfer_id" not in body and "data_b64" not in body, body
                streamed = [n for n in cloud.notifications if n.get("method") == "chunk"]
                assert not streamed, f"超限产物不得流出任何 chunk: {len(streamed)}"
                _trace("test: oversize rejected before stream ok")
            finally:
                cfg._config["client_exec"]["max_payload_mb"] = saved
                await worker.stop()

    asyncio.run(scenario())


def test_fs_long_production_emits_progress(exec_env):
    """长生产活性：打包期间每 interval 发 progress {call_id, note}（恰两键，call_id=请求 id）。"""
    import threading

    import app.services.workspace_files_service as wfs
    import app.services.device_executor_service as des
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "prog"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()

    async def scenario():
        real_zip = wfs.build_folder_zip
        real_interval = des._progress_interval

        def slow_zip(root, target, dest):
            started.set()
            if not release.wait(timeout=20):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                des._progress_interval = lambda w: 0.2
                task = asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "prog"}, rid="prog1"))
                await asyncio.to_thread(started.wait, 10)
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline:
                    got = [n for n in cloud.notifications if n.get("method") == "progress"]
                    if len(got) >= 2:
                        break
                    await asyncio.sleep(0.05)
                got = [n for n in cloud.notifications if n.get("method") == "progress"]
                assert len(got) >= 2, f"长生产应发≥2 条 progress，实际 {len(got)}"
                for n in got:
                    assert set(n["params"].keys()) == {"call_id", "note"}, n
                    assert n["params"]["call_id"] == "prog1", n
                release.set()
                resp = await asyncio.wait_for(task, timeout=15)
                assert resp["result"]["ok"] is True, resp
                # 幽灵 ticker 钉（评审 M5）：生产终止后必须停发（3×interval 无新帧）
                baseline_ticks = len([n for n in cloud.notifications if n.get("method") == "progress"])
                await asyncio.sleep(0.6)
                after_ticks = len([n for n in cloud.notifications if n.get("method") == "progress"])
                assert after_ticks == baseline_ticks, (
                    f"生产结束后 progress 必须停发: {baseline_ticks} -> {after_ticks}")
                _trace(f"test: progress ticks={baseline_ticks} ok")
            finally:
                wfs.build_folder_zip = real_zip
                des._progress_interval = real_interval
                release.set()
                await worker.stop()

    asyncio.run(scenario())


# ---------------- W19 补波 3：progress 发报对齐（间隔/note/覆盖）----------------

def test_progress_interval_matches_spec_formula():
    """间隔=min(30s, 窗/3)，下限 1s（任务单 §一.2）。"""
    from app.services.device_executor_service import _progress_interval

    assert _progress_interval(120.0) == 30.0      # fs/* 窗（缺省）
    assert _progress_interval(30.0) == 10.0
    assert _progress_interval(9.0) == 3.0
    assert _progress_interval(3600.0) == 30.0     # 上限封顶
    assert _progress_interval(0.5) == 1.0         # ≤1 次/秒下限
    # 非法窗（0/None/非数）一律回落缺省 120s 窗（与 None 同处理，非 floor）
    assert _progress_interval(0) == 30.0 and _progress_interval(None) == 30.0


def test_sanitize_note_single_line_truncated():
    """note=单行 ≤60 无控制字符（任务单 §一.3）。"""
    from app.services.device_executor_service import _sanitize_note

    assert _sanitize_note("zip: 12/50 files") == "zip: 12/50 files"
    out = _sanitize_note("a\nb\x00c\td")
    assert "\n" not in out and "\x00" not in out and "\t" not in out, out
    # C1 控制符/bidi/零宽同样清除（评审 B-M3）
    out = _sanitize_note("a\u0080b\u202ec\u200bd")
    assert all(ch.isprintable() and ch != "\u202e" for ch in out), repr(out)
    assert len(_sanitize_note("x" * 200)) == 60
    assert _sanitize_note(None) == "" and _sanitize_note("  ") == ""


def test_fs_download_emits_progress(exec_env):
    """覆盖面（任务单 §一.4 建议档）：fs/download 长传输发 progress（call_id=请求 id）。"""
    import app.services.device_executor_service as des
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "slow.bin").write_bytes(b"y" * 4096)

    started = asyncio.Event()
    release = asyncio.Event()

    async def scenario():
        worker = DeviceExecutorWorker()
        real_send = worker._send_chunks
        real_interval = des._progress_interval

        async def slow_send(ws, req_id, call_id, target):
            started.set()
            await release.wait()
            return await real_send(ws, req_id, call_id, target)

        worker._send_chunks = slow_send
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                des._progress_interval = lambda w: 0.2
                task = asyncio.ensure_future(
                    cloud.request("fs/download", {"path": "slow.bin"}, rid="dl-prog"))
                await asyncio.wait_for(started.wait(), timeout=10)
                deadline = time_mod.monotonic() + 5
                while time_mod.monotonic() < deadline:
                    if len([n for n in cloud.notifications if n.get("method") == "progress"]) >= 2:
                        break
                    await asyncio.sleep(0.05)
                got = [n for n in cloud.notifications if n.get("method") == "progress"]
                assert len(got) >= 2, f"download 应发 progress，实际 {len(got)}"
                for n in got:
                    assert set(n["params"].keys()) == {"call_id", "note"}, n
                    assert n["params"]["call_id"] == "dl-prog", n
                release.set()
                resp = await asyncio.wait_for(task, timeout=15)
                assert resp["result"]["ok"] is True, resp
                await asyncio.sleep(0.2)  # 排空 cancel 前已在途的 tick
                baseline = len([n for n in cloud.notifications if n.get("method") == "progress"])
                await asyncio.sleep(0.6)  # 3×interval：终态后必须停发（幽灵 ticker 钉）
                after = len([n for n in cloud.notifications if n.get("method") == "progress"])
                assert after == baseline, f"终态后 progress 必须停发: {baseline} -> {after}"
                _trace(f"test: download progress ticks={len(got)} ok")
            finally:
                des._progress_interval = real_interval
                release.set()
                await worker.stop()

    asyncio.run(scenario())


def test_tools_call_emits_progress_for_long_run(exec_env):
    """覆盖面（建议档）：tools/call 长任务发 progress（call_id=请求 id）；快速调用自然不发。"""
    import app.services.device_executor_service as des
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        worker = DeviceExecutorWorker()
        real_interval = des._progress_interval
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                des._progress_interval = lambda w: 0.2
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "sleep 1.2 && echo done"},
                    "timeout_seconds": 30,
                }, rid="tc-prog")
                assert resp["result"]["ok"] is True, resp
                got = [n for n in cloud.notifications if n.get("method") == "progress"]
                assert len(got) >= 2, f"长 tools/call 应发 progress，实际 {len(got)}"
                for n in got:
                    assert set(n["params"].keys()) == {"call_id", "note"}, n
                    assert n["params"]["call_id"] == "tc-prog", n
                    assert len(n["params"]["note"]) <= 60 and "\n" not in n["params"]["note"], n
                await asyncio.sleep(0.2)  # 排空 cancel 前已在途的 tick
                after_base = len([n for n in cloud.notifications if n.get("method") == "progress"])
                await asyncio.sleep(0.6)  # 3×interval：终态后必须停发（幽灵 ticker 钉）
                after = len([n for n in cloud.notifications if n.get("method") == "progress"])
                assert after == after_base, f"终态后 progress 必须停发: {after_base} -> {after}"
                _trace(f"test: tools/call progress ticks={len(got)} ok")
            finally:
                des._progress_interval = real_interval
                await worker.stop()

    asyncio.run(scenario())


def test_progress_interval_first_tick_within_half_window():
    """首 tick ≤ 窗/2（任务单 §一.2）：对一切现实窗（≥3s）成立。"""
    from app.services.device_executor_service import _progress_interval

    for window in (3, 6, 12, 30, 60, 120, 240, 1800):
        assert _progress_interval(window) <= window / 2, window


def test_progress_ticker_survives_note_fn_failure(exec_env):
    """note_fn 故障不得杀死发报循环（评审 B-M4：静默断续期）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def scenario():
        sent: list = []

        class W:
            async def send(self, raw):
                sent.append(json.loads(raw))

        worker = DeviceExecutorWorker()

        def bad_note():
            raise RuntimeError("boom")

        task = asyncio.ensure_future(
            worker._progress_ticker(W(), "cid-1", bad_note, 3.0))
        await asyncio.sleep(1.4)
        task.cancel()
        assert len(sent) >= 1, "note_fn 抛异常后发报必须继续"
        assert sent[0]["params"] == {"call_id": "cid-1", "note": ""}, sent[0]
        _trace("test: ticker survives note_fn failure ok")

    asyncio.run(scenario())


def test_fs_office_convert_timeout_tracks_budget(exec_env):
    """B-I2 钉：RPC 路径 office 转换超时=call_max_extension_seconds；
    0 时回退 office_preview_timeout_seconds（D-94：不限制不延伸到子进程）。"""
    import app.services.office_preview_service as ops
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    (exec_env.ws_root / "doc.docx").write_bytes(b"x")

    async def scenario():
        cfg = get_config()
        saved = cfg._config["client_exec"].get("call_max_extension_seconds")
        real = ops.convert_workspace_document
        real_soffice = ops.ensure_soffice
        captured: dict = {}

        async def fake_convert(src, **kw):
            captured.update(kw)
            raise ops.OfficePreviewFailed("stop-here")

        async def fake_soffice(preferred="soffice"):
            return "soffice-stub"  # 本腿钉取值，不依赖宿主 LibreOffice（复评 r2 I-2）

        ops.convert_workspace_document = fake_convert
        ops.ensure_soffice = fake_soffice
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                for rid, budget, expect in (("bt1", 3600, 3600),
                                            ("bt2", 7200, 7200),
                                            ("bt3", 0, int(cfg.office_preview_timeout_seconds))):
                    cfg._config["client_exec"]["call_max_extension_seconds"] = budget
                    captured.clear()
                    resp = await cloud.request("fs/office-pdf", {"path": "doc.docx"}, rid=rid)
                    assert "office_preview_failed" in resp["result"]["error"], resp
                    assert captured.get("timeout") == expect, (budget, captured.get("timeout"))
            finally:
                ops.convert_workspace_document = real
                ops.ensure_soffice = real_soffice
                if saved is None:
                    cfg._config["client_exec"].pop("call_max_extension_seconds", None)
                else:
                    cfg._config["client_exec"]["call_max_extension_seconds"] = saved
                await worker.stop()

    asyncio.run(scenario())


def test_fs_zip_budget_zero_means_unlimited(exec_env):
    """D-94 钉：call_max_extension_seconds=0 → 打包不设本地限时（交云端 cancel 收口）。"""
    import threading

    import app.services.workspace_files_service as wfs
    from app.core.config import get_config
    from app.services.device_executor_service import DeviceExecutorWorker

    pack = exec_env.ws_root / "unl"
    pack.mkdir()
    (pack / "a.txt").write_text("x", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()

    async def scenario():
        real_zip = wfs.build_folder_zip
        cfg = get_config()
        saved = cfg._config["client_exec"].get("call_max_extension_seconds")
        saved_to = cfg._config["client_exec"].get("call_timeout_seconds")
        cfg._config["client_exec"]["call_max_extension_seconds"] = 0
        # 复评 r2 M2：call_timeout=5 作对照预算——打包 >5s 才能证伪「预算误用 call_timeout」
        cfg._config["client_exec"]["call_timeout_seconds"] = 5

        def slow_zip(root, target, dest):
            started.set()
            if not release.wait(timeout=15):
                raise TimeoutError("release not signaled")
            return real_zip(root, target, dest)

        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                wfs.build_folder_zip = slow_zip
                task = asyncio.ensure_future(
                    cloud.request("fs/zip", {"path": "unl"}, rid="unl1", timeout=15))
                await asyncio.to_thread(started.wait, 10)
                asyncio.get_event_loop().call_later(6.0, release.set)  # >5s：预算=0 不得裁
                resp = await asyncio.wait_for(task, timeout=15)
                assert resp["result"]["ok"] is True, resp  # 未被本地限时裁掉
                assert "timeout" not in str(resp["result"]), resp
                _trace("test: zip budget 0 = unlimited ok")
            finally:
                wfs.build_folder_zip = real_zip
                release.set()
                if saved is None:
                    cfg._config["client_exec"].pop("call_max_extension_seconds", None)
                else:
                    cfg._config["client_exec"]["call_max_extension_seconds"] = saved
                if saved_to is None:
                    cfg._config["client_exec"].pop("call_timeout_seconds", None)
                else:
                    cfg._config["client_exec"]["call_timeout_seconds"] = saved_to
                await worker.stop()

    asyncio.run(scenario())


# ---------------- W20 身份绑定 + 名门按云端身份（PLAN_device_workspace_identity_impl T3）----------------

class _FakeAsyncDB:
    """最小 async 会话假件：get(User|SyncState, pk) + execute(select SyncState)。"""

    def __init__(self, users, sync_rows):
        self.users = {str(u.id): u for u in users}
        self.sync_rows = list(sync_rows)

    async def get(self, model, key):
        name = getattr(model, "__name__", "")
        if name == "User":
            return self.users.get(str(key))
        if name == "SyncState":
            for r in self.sync_rows:
                if str(r.user_id) == str(key):
                    return r
            return None
        return None

    async def execute(self, stmt):
        rows = list(self.sync_rows)

        class _Res:
            def scalars(self_inner):
                class _S:
                    def first(self_s):
                        return rows[0] if rows else None
                return _S()
        return _Res()


def test_resolve_identity_prefers_pair_local_user_id():
    """配对记录 local_user_id 优先——不得回落「首个用户」（workspace_mismatch 事故根因）。"""
    from types import SimpleNamespace as NS

    from app.services.device_executor_service import resolve_execution_identity

    first = NS(id="uid-first", username="bob")
    bound = NS(id="uid-bound", username="alice")
    db = _FakeAsyncDB([first, bound], [])
    binding = {"local_user_id": "uid-bound", "token": "t"}

    owner, expected = asyncio.run(resolve_execution_identity(binding, db))
    assert owner.id == "uid-bound", f"必须取配对属主，实际 {owner.id!r}"
    assert expected is None, "无云端身份时期望值必须为 None（名门 fail-closed）"


def test_resolve_identity_falls_back_to_sync_binding():
    """无 local_user_id 时按 cloud_username 匹配 sync 绑定映射本地用户。"""
    from types import SimpleNamespace as NS

    from app.services.device_executor_service import resolve_execution_identity

    first = NS(id="uid-first", username="bob")
    bound = NS(id="uid-bound", username="alice")
    st = NS(user_id="uid-bound", cloud_username="alice",
            cloud_user_id="cafef00d-0000-4000-8000-000000000000")
    db = _FakeAsyncDB([first, bound], [st])
    binding = {"cloud_username": "alice", "token": "t"}

    owner, expected = asyncio.run(resolve_execution_identity(binding, db))
    assert owner.id == "uid-bound", f"sync 绑定必须映射到绑定用户，实际 {owner.id!r}"
    assert expected == "alice-cafef00d", f"期望值须按云端身份导出，实际 {expected!r}"


def test_resolve_identity_fail_closed_when_unbound():
    """无任何绑定 → identity_unbound（fail-closed，不回落默认用户）。"""
    import pytest as _pytest

    from app.services.device_executor_service import IdentityUnboundError, resolve_execution_identity

    db = _FakeAsyncDB([__import__("types").SimpleNamespace(id="uid-first", username="bob")], [])
    with _pytest.raises(IdentityUnboundError, match="未绑定"):
        asyncio.run(resolve_execution_identity({"token": "t"}, db))


def test_expected_ctx_derived_from_pair_identity():
    """pair 记录携带云端身份时期望值直接导出（不依赖 sync 绑定）。"""
    from types import SimpleNamespace as NS

    from app.services.device_executor_service import resolve_execution_identity

    owner_row = NS(id="uid-bound", username="alice")
    db = _FakeAsyncDB([owner_row], [])
    binding = {"local_user_id": "uid-bound", "token": "t",
               "cloud_username": "alice",
               "cloud_user_id": "cafef00d-0000-4000-8000-000000000000"}

    owner, expected = asyncio.run(resolve_execution_identity(binding, db))
    assert owner.id == "uid-bound"
    assert expected == "alice-cafef00d", f"期望值须=云端身份导出 {{slug}}-{{uid8}}，实际 {expected!r}"


def test_identity_unbound_when_no_cloud_identity_and_ctx_present(exec_env, monkeypatch):
    """名门：ws_ctx 有值但设备无云端身份 → identity_unbound（不误判 workspace_mismatch）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def _no_cloud_identity(worker_self, binding=None):
        return exec_env.owner, str(exec_env.ws_root.resolve()), None

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _no_cloud_identity)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo hi"},
                    "timeout_seconds": 20,
                    "workspace_ctx": "alice-cafef00d",
                }, rid="c-unbound")
                assert resp["result"]["ok"] is False, resp
                assert "identity_unbound" in resp["result"]["error"], resp
                _trace("test: identity_unbound fail-closed ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_workspace_gate_accepts_matching_expected_ctx(exec_env, monkeypatch):
    """名门：ws_ctx == 云端身份期望值 → 放行执行（跨栈 uuid 失配的根治面）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def _cloud_identity(worker_self, binding=None):
        return exec_env.owner, str(exec_env.ws_root.resolve()), "alice-cafef00d"

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _cloud_identity)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo gate-ok"},
                    "timeout_seconds": 20,
                    "workspace_ctx": "alice-cafef00d",
                }, rid="c-match")
                assert resp["result"]["ok"] is True, resp
                assert "gate-ok" in resp["result"]["content"], resp
                _trace("test: workspace gate accepts cloud-identity expected ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


# ---------------- W20 fix-round（双评审采纳：wire code / falsy 名门 / 真会话 / 快照）----------------

def test_unbound_tools_call_wire_code_is_identity_unbound(exec_env, monkeypatch):
    """r2-F1/r1-F1：resolve 抛 identity_unbound 时 wire code 必须=identity_unbound
    （不得被 catch-all 降级 workspace_unavailable），且错误串无双前缀。"""
    from app.services.device_executor_service import DeviceExecutorWorker, IdentityUnboundError

    async def _unbound(worker_self, binding=None):
        raise IdentityUnboundError("设备未绑定执行身份——请重新配对（或重新提交同步绑定）")

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _unbound)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo hi"},
                    "timeout_seconds": 20,
                }, rid="c-wire-unbound")
                assert resp["result"]["ok"] is False, resp
                err = resp["result"]["error"]
                assert err.startswith("identity_unbound:"), f"wire code 必须=identity_unbound，实际 {err!r}"
                assert "workspace_unavailable" not in err, err
                assert "identity_unbound: identity_unbound" not in err, f"双前缀 {err!r}"
                _trace("test: identity_unbound wire code ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_workspace_ctx_empty_or_null_rejected(exec_env, monkeypatch):
    """r2-F3/r1-F2：键在但空串/null 不得按「缺省」放行（falsy 绕过面）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def _with_expected(worker_self, binding=None):
        return exec_env.owner, str(exec_env.ws_root.resolve()), "alice-cafef00d"

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _with_expected)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                for bad in ("", None):
                    resp = await cloud.request("tools/call", {
                        "tool": "terminal",
                        "args": {"command": "echo hi"},
                        "timeout_seconds": 20,
                        "workspace_ctx": bad,
                    }, rid=f"c-falsy-{str(bad) or 'empty'}")
                    assert resp["result"]["ok"] is False, (bad, resp)
                    assert "workspace_mismatch" in resp["result"]["error"], (bad, resp)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo absent-ok"},
                    "timeout_seconds": 20,
                }, rid="c-absent")
                assert resp["result"]["ok"] is True, resp  # 键缺省（真旧云端）仍放行
                _trace("test: falsy workspace_ctx rejected, absent-key passes")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_resolve_identity_real_session_filters_and_orders():
    """r2-F6/r1-F4：真 SQLite 会话腿——WHERE 按 cloud_username 过滤 + 多行确定序
    （fake 不过滤 WHERE 的假绿面由本腿钉死）。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.db.database import SyncState, User
    from app.services.device_executor_service import resolve_execution_identity

    engine = create_engine("sqlite:///:memory:")
    # 只建所需两表——全 metadata 含 sync_events.payload JSONB 无法建于 SQLite
    User.__table__.create(engine)
    SyncState.__table__.create(engine)
    sess = Session(engine)
    u1 = User(id="uid-first", username="bob", password_hash="x")
    u2 = User(id="uid-bound", username="alice", password_hash="x")
    sess.add_all([u1, u2])
    # 两行同 cloud_username：仅 uid-bound 行匹配绑定；另有他账号行
    sess.add_all([
        SyncState(user_id="uid-first", device_id="d1", cloud_username="other-account"),
        SyncState(user_id="uid-bound", device_id="d2", cloud_username="alice",
                  cloud_user_id="cafef00d-0000-4000-8000-000000000000", sync_enabled=True),
    ])
    sess.commit()

    class _Bridge:
        def __init__(self, s):
            self._s = s

        async def execute(self, stmt):
            return self._s.execute(stmt)

        async def get(self, model, key):
            return self._s.get(model, key)

        async def commit(self):
            self._s.commit()

    db = _Bridge(sess)
    owner, expected = asyncio.run(resolve_execution_identity(
        {"cloud_username": "alice", "token": "t"}, db))
    assert owner.id == "uid-bound", f"WHERE 必须过滤 cloud_username，实际 {owner.id!r}"
    assert expected == "alice-cafef00d", expected

    # 多行同名 → 确定序（user_id 排序取首）：两新用户绑同一云端账号名
    u3 = User(id="uid-aaa", username="alice", password_hash="x")
    u4 = User(id="uid-zzz", username="zed", password_hash="x")
    sess.add_all([u3, u4])
    sess.add(SyncState(user_id="uid-zzz", device_id="d3", cloud_username="dup",
                       cloud_user_id="aaaaaaaa-0000-4000-8000-000000000000"))
    sess.add(SyncState(user_id="uid-aaa", device_id="d4", cloud_username="dup",
                       cloud_user_id="bbbbbbbb-0000-4000-8000-000000000000"))
    sess.commit()
    owner2, _ = asyncio.run(resolve_execution_identity({"cloud_username": "dup", "token": "t"}, db))
    assert owner2.id == "uid-aaa", f"多行同名必须按 user_id 确定序，实际 {owner2.id!r}"
    sess.close()
    _trace("test: real-session WHERE filter + deterministic order ok")


def test_resolve_identity_wrapper_returns_expected(tmp_path, monkeypatch):
    """r2-F6：_resolve_identity 包装体（load_binding→会话→ensure→三元组）零覆盖面收口。
    不用 exec_env——该夹具打桩了 _resolve_identity 本体（测不到包装）。"""
    from types import SimpleNamespace as NS

    from app.db import database as dbmod
    from app.services.device_executor_service import DeviceExecutorWorker

    monkeypatch.setenv("LOCALSTACK_HOME", str(tmp_path))
    monkeypatch.delenv("WEAVE_LOCALSTACK_HOME", raising=False)
    path = tmp_path / "app" / "device_exec.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "token": "t", "device_id": "d", "local_user_id": "uid-bound",
        "cloud_username": "alice",
        "cloud_user_id": "cafef00d-0000-4000-8000-000000000000",
    }), encoding="utf-8")

    class _DB:
        async def get(self, model, key):
            return NS(id="uid-bound", username="alice") if getattr(model, "__name__", "") == "User" else None

        async def execute(self, stmt):
            class _R:
                def scalars(self):
                    class _S:
                        def first(self):
                            return None
                    return _S()
            return _R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbmod, "AsyncSessionLocal", lambda: _DB())

    ws_dir = tmp_path / "ws" / "alice-beef0002"
    ws_dir.mkdir(parents=True)

    async def _fake_ensure(db, user_id, username=None):
        return NS(root_path=str(ws_dir.resolve()))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _fake_ensure)

    owner, ws_root, expected = asyncio.run(DeviceExecutorWorker()._resolve_identity())
    assert owner.id == "uid-bound"
    assert expected == "alice-cafef00d", expected
    assert ws_root == str(ws_dir.resolve()), ws_root
    _trace("test: _resolve_identity wrapper ok")


def test_inflight_call_uses_binding_snapshot(exec_env, monkeypatch):
    """r2-F7/r1-F11：调用中途换绑不得偷换在途身份——resolve 用发起时快照。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    seen = {}

    async def _snap(worker_self, binding=None):
        seen["local_user_id"] = (binding or {}).get("local_user_id")
        return exec_env.owner, str(exec_env.ws_root.resolve()), "alice-cafef00d"

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _snap)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            path = exec_env.tmp_path / "app" / "device_exec.json"
            rec = json.loads(path.read_text(encoding="utf-8"))
            rec["local_user_id"] = "uid-original"
            path.write_text(json.dumps(rec), encoding="utf-8")
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                resp = await cloud.request("tools/call", {
                    "tool": "terminal",
                    "args": {"command": "echo snap-ok"},
                    "timeout_seconds": 20,
                    "workspace_ctx": "alice-cafef00d",
                }, rid="c-snap")
                assert resp["result"]["ok"] is True, resp
                assert seen.get("local_user_id") == "uid-original", (
                    f"在途调用必须用发起时绑定快照，实际 {seen!r}")
                _trace("test: inflight binding snapshot ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())


def test_workspace_ctx_non_string_rejected(exec_env, monkeypatch):
    """复评 NEW-5：非字符串 ws_ctx（123/{}）同样拒绝（代码已覆盖，本腿钉死）。"""
    from app.services.device_executor_service import DeviceExecutorWorker

    async def _with_expected(worker_self, binding=None):
        return exec_env.owner, str(exec_env.ws_root.resolve()), "alice-cafef00d"

    monkeypatch.setattr(DeviceExecutorWorker, "_resolve_identity", _with_expected)

    async def scenario():
        async with FakeCloud() as cloud:
            _write_binding(exec_env.tmp_path, cloud.port)
            worker = DeviceExecutorWorker()
            await worker.start()
            try:
                await asyncio.wait_for(cloud.connected.wait(), timeout=10)
                for bad in (123, {"a": 1}):
                    resp = await cloud.request("tools/call", {
                        "tool": "terminal",
                        "args": {"command": "echo hi"},
                        "timeout_seconds": 20,
                        "workspace_ctx": bad,
                    }, rid=f"c-nonstr-{type(bad).__name__}")
                    assert resp["result"]["ok"] is False, (bad, resp)
                    assert "workspace_mismatch" in resp["result"]["error"], (bad, resp)
                _trace("test: non-string workspace_ctx rejected ok")
            finally:
                await worker.stop()

    asyncio.run(scenario())
