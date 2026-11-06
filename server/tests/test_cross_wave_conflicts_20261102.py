"""RED tests (A4.8) — 跨波冲突修复波 C1-C7（2026-11-02）。

审计报告 docs/AUDIT_20261102_weekly_blindspots_conflicts.md §3.3 的七项跨波冲突：
  C1  manage_mcp_server 未入 _AGENT_TOOLS → classify_tool_result 兜底 UNTRUSTED
      → TaintLedger.max_taint 单调不可逆下降 → enforce 下 act gate 自锁。
  C2  用户级 HTTP MCP 的运行期私网判定只做字面量（无 DNS），弱于写入期。
  C2b MCP HTTP 走共享 client 的 follow_redirects=True → 公网 302 跳内网不被拦。
  C3  _OAUTH_BY_URL / _SESSION_CACHE 按 URL 单键 → 用户服务器消费运营方 OAuth
      bearer 并丢弃自己的 api_key。
  C4  _save_assistant_terminal 无条件 row.tool_calls = tool_calls；stop 与
      legacy-resume 传 None → 抹掉 W11 写入的在途行工具证据。
  C5  「空壳」判据两套（_has_turn_evidence 计 tool_calls/reasoning vs
      _delete_empty_streaming_row 只计 content）→ 同形状行 stop 删 / sweep 留。
  C6  legacy-resume 终态落库未传 row_id → 回退「最新 streaming 行」反查。
  C7  根级 output_files/ 未进 strip 排除清单 → run.sh 命中 FORBID → strip exit 2。

Run: cd backend && python -m pytest tests/test_cross_wave_conflicts_20261102.py -v
"""
import ast
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event as sa_event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_BASE = datetime(2026, 11, 2, 12, 0, 0)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# C1 — taint classification
# --------------------------------------------------------------------------

def test_c1_manage_mcp_server_is_not_untrusted():
    """C1: agent 自产的 MCP 管理结果不得把会话 taint 拖到 untrusted。"""
    from app.services import taint_service as ts

    assert "manage_mcp_server" in ts._AGENT_TOOLS, (
        "manage_mcp_server 必须与同构的 skill_manage 一样列入 _AGENT_TOOLS，"
        "否则 classify_tool_result 兜底 UNTRUSTED，enforce 下自锁 act gate"
    )
    assert ts.classify_tool_result(
        "manage_mcp_server", json.dumps({"ok": True}), input_taint=ts.TAINT_AGENT
    ) != ts.TAINT_UNTRUSTED


def test_c1_agent_tools_never_raise_taint():
    """C1 反向约束：列入 _AGENT_TOOLS 只能封顶为 agent，绝不抬高既有的 untrusted。"""
    from app.services import taint_service as ts

    assert ts.classify_tool_result(
        "manage_mcp_server", "x", input_taint=ts.TAINT_UNTRUSTED
    ) == ts.TAINT_UNTRUSTED


# --------------------------------------------------------------------------
# C2 — runtime private-host check must be DNS-aware (same source as write time)
# --------------------------------------------------------------------------

def test_c2_runtime_private_host_resolves_dns(monkeypatch):
    """C2: 域名解析到私网地址时，运行期判定必须拦（字面量判据漏这种形态）。"""
    import app.tools.mcp_client as mc

    async def _fake_getaddrinfo(host, port, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    class _Loop:
        async def getaddrinfo(self, *a, **k):
            return await _fake_getaddrinfo(*a, **k)

    monkeypatch.setattr(mc.asyncio, "get_running_loop", lambda: _Loop())

    assert _run(mc._user_mcp_host_blocked("internal.example")) is True


def test_c2_runtime_public_host_allowed(monkeypatch):
    """C2: 合法公网域名不被误伤。"""
    import app.tools.mcp_client as mc

    class _Loop:
        async def getaddrinfo(self, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(mc.asyncio, "get_running_loop", lambda: _Loop())

    assert _run(mc._user_mcp_host_blocked("example.com")) is False


def test_c2_runtime_loopback_literal_still_blocked():
    """C2: 字面量私网/回环仍然拦（不因换成 DNS 版而漏掉原有形态）。"""
    import app.tools.mcp_client as mc

    assert _run(mc._user_mcp_host_blocked("127.0.0.1")) is True
    assert _run(mc._user_mcp_host_blocked("localhost")) is True
    assert _run(mc._user_mcp_host_blocked("169.254.169.254")) is True


# --------------------------------------------------------------------------
# C2b — redirect hops must be validated, legal public redirects still followed
# --------------------------------------------------------------------------

def _harden_private_hosts(monkeypatch):
    """强制 [security].user_mcp_allow_private_hosts=false（公开部署姿态）。"""
    import app.tools.mcp_client as mc

    class _Cfg:
        user_mcp_allow_private_hosts = False
    monkeypatch.setattr(mc, "get_config", lambda: _Cfg())


def test_c2b_redirect_to_private_rejected(monkeypatch):
    """C2b: 公网 URL 302 跳内网必须拒绝（硬化的公开部署姿态）。"""
    import app.tools.mcp_client as mc

    _harden_private_hosts(monkeypatch)
    assert mc._mcp_redirect_target_blocked("https://evil.example/mcp", "http://127.0.0.1:47321/mcp") is True
    assert mc._mcp_redirect_target_blocked("https://evil.example/mcp", "http://10.0.0.5/mcp") is True


def test_c2b_same_origin_redirect_allowed(monkeypatch):
    """C2b（评审 I4）：同源跳转即便目标是 loopback 也必须放行。

    运营方自己的 remote MCP 在 `http://127.0.0.1:47321/mcp`；若它把路径规范化
    成 `/mcp/`，旧实现会当「跳私网」拦掉 → 既有能力回退。
    """
    import app.tools.mcp_client as mc

    _harden_private_hosts(monkeypatch)
    assert mc._mcp_redirect_target_blocked(
        "http://127.0.0.1:47321/mcp", "/mcp/"
    ) is False
    assert mc._mcp_redirect_target_blocked(
        "http://127.0.0.1:47321/mcp", "http://127.0.0.1:47321/mcp/"
    ) is False


def test_c2b_private_redirect_allowed_when_opted_in(monkeypatch):
    """C2b（评审 I4）：已显式 opt-in 私网时，跨源私网跳转也放行（与入口门禁同口径）。"""
    import app.tools.mcp_client as mc

    class _Cfg:
        user_mcp_allow_private_hosts = True
    monkeypatch.setattr(mc, "get_config", lambda: _Cfg())
    assert mc._mcp_redirect_target_blocked(
        "https://evil.example/mcp", "http://127.0.0.1:47321/mcp"
    ) is False


def test_c2b_public_redirect_allowed():
    """C2b 不回归：合法公网重定向仍可跟随。"""
    import app.tools.mcp_client as mc

    assert mc._mcp_redirect_target_blocked("http://a.example/mcp", "https://a.example/mcp") is False
    assert mc._mcp_redirect_target_blocked("http://a.example/mcp", "https://b.example/mcp") is False


def test_c2b_mcp_posts_disable_auto_redirect():
    """C2b: MCP 的 HTTP 调用不得让共享 client 自动跟随重定向（必须逐跳校验）。

    注意：OAuth **token endpoint** 的 post 不在此列——它由运营方
    `oauth_state_file` 配置，非用户输入，且使用独立 client。
    """
    src = (_BACKEND / "app" / "tools" / "mcp_client.py").read_text(encoding="utf-8")
    assert "await client.post(server_url" not in src, (
        "MCP 服务器请求仍直接走共享 client（follow_redirects=True）"
    )
    assert src.count("await _mcp_post(client, server_url") >= 4, (
        "4 处 MCP HTTP 调用（initialize / initialized 通知 / tools-list / call）"
        "都应走逐跳校验的 _mcp_post"
    )
    helper = re.search(r"async def _mcp_post\(.*?\n(?=\ndef |\nasync def )", src, re.S)
    assert helper and "follow_redirects=False" in helper.group(0), (
        "_mcp_post 内部必须显式 follow_redirects=False 再逐跳校验"
    )


def test_c2b_mcp_post_follows_public_redirect(monkeypatch):
    """C2b 不回归（端到端）：合法公网重定向必须被跟随到最终 200。"""
    import app.tools.mcp_client as mc

    # 本机 DNS 把 *.example 劫持到 198.18.0.0/15（保留段，private 判定=true）；
    # 该用例验证的是“公网跳转被跟随”的分支，不依赖真实 DNS——桩掉主机解析。
    async def _public(host):
        return False
    monkeypatch.setattr(mc, "_user_mcp_host_blocked", _public)

    class _Resp:
        def __init__(self, code, loc=None):
            self.status_code = code
            self.headers = {"location": loc} if loc else {}

    class _Client:
        def __init__(self):
            self.seen = []

        async def post(self, url, **kw):
            self.seen.append(url)
            assert kw.get("follow_redirects") is False, "不得让 client 自动跟随"
            if "a.example" in url:
                return _Resp(302, "https://b.example/mcp")
            return _Resp(200)

    c = _Client()
    resp = _run(mc._mcp_post(c, "http://a.example/mcp", json={"x": 1}))
    assert resp.status_code == 200
    assert c.seen == ["http://a.example/mcp", "https://b.example/mcp"]


def test_c2b_mcp_post_blocks_private_redirect(monkeypatch):
    """C2b（端到端）：公网 302 跳内网必须停在 3xx，不发起第二跳。"""
    import app.tools.mcp_client as mc
    _harden_private_hosts(monkeypatch)

    class _Resp:
        def __init__(self, code, loc=None):
            self.status_code = code
            self.headers = {"location": loc} if loc else {}

    class _Client:
        def __init__(self):
            self.seen = []

        async def post(self, url, **kw):
            self.seen.append(url)
            return _Resp(302, "http://127.0.0.1:47321/mcp")

    c = _Client()
    resp = _run(mc._mcp_post(c, "https://evil.example/mcp", json={"x": 1}))
    assert resp.status_code == 302, "跳内网必须被拦在 3xx"
    assert c.seen == ["https://evil.example/mcp"], "不得向私网发起第二跳"


# --------------------------------------------------------------------------
# C3 — OAuth / session cache must be owner-scoped
# --------------------------------------------------------------------------

def test_c3_cache_key_scopes_owner():
    """C3: 同一 URL 下不同 owner 不得共享 MCP session。"""
    import app.tools.mcp_client as mc

    assert mc._cache_key("http://x/mcp", {}, "u1") != mc._cache_key("http://x/mcp", {}, "u2")
    assert mc._cache_key("http://x/mcp", {}, "u1") != mc._cache_key("http://x/mcp", {}, "")


def test_c3_user_never_consumes_global_oauth(monkeypatch):
    """C3: 用户级服务器不得继承全局 OAuth provider 的 bearer token。"""
    import app.tools.mcp_client as mc

    class _P:
        async def token(self, force=False):
            return "OPERATOR-TOKEN"

    monkeypatch.setattr(mc, "_OAUTH_BY_URL", {("http://127.0.0.1:47321/mcp", ""): _P()})

    global_hdrs = _run(mc._effective_headers("http://127.0.0.1:47321/mcp", {}, owner=""))
    user_hdrs = _run(mc._effective_headers("http://127.0.0.1:47321/mcp", {"Authorization": "Bearer USERKEY"}, owner="u1"))

    assert global_hdrs.get("Authorization") == "Bearer OPERATOR-TOKEN", "全局路径行为不变"
    assert user_hdrs.get("Authorization") == "Bearer USERKEY", (
        "用户级服务器必须使用自己的 api_key，不得被运营方 OAuth 覆盖"
    )


def test_c3_probe_is_owner_scoped(monkeypatch):
    """C3（评审 I1）：**探针**路径也不得消费全局 OAuth bearer。

    `_probe_mcp_spec` 是 user sync 的必走路径；它此前恒用 `("url","")` 查
    OAuth —— 用户级 spec 在同一 URL 上会拿到运营方 bearer，同时因该键存在
    而丢掉自己的 api_key。
    """
    import app.tools.mcp_client as mc

    class _P:
        async def token(self, force=False):
            return "OPERATOR-TOKEN"

    monkeypatch.setattr(mc, "_OAUTH_BY_URL", {("http://127.0.0.1:47321/mcp", ""): _P()})

    seen = {}

    async def _fake_post(client, url, *, json=None, headers=None, timeout=15.0, max_hops=3):
        seen["auth"] = (headers or {}).get("Authorization")
        class _R:
            status_code = 200
            headers = {"mcp-session-id": "sid"}
            text = '{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"t","inputSchema":{}}]}}'
            def json(self):
                return {"result": {"tools": [{"name": "t", "inputSchema": {}}]}}
        return _R()

    monkeypatch.setattr(mc, "_mcp_post", _fake_post)

    _user_key = "<user-own-key>"          # 非真实凭据：占位符形态，secret-scan 豁免
    spec = {"transport": "http", "url": "http://127.0.0.1:47321/mcp",
            "api_key": _user_key, "raw": {}}
    _run(mc._probe_mcp_spec("x", spec, owner="u1"))
    assert seen["auth"] == "Bearer " + _user_key, (
        "探针不得携带运营方 OAuth bearer；且用户自己的 api_key 不得被丢弃"
    )


def test_c3_host_check_runs_before_reload_lock():
    """C3/C2（评审 I3）：DNS 私网判定必须在进程级 reload 锁**之外**。

    锁内做 DNS 会让一个不可达域名阻塞所有用户的 run 启动
    （ensure_user_mcp_tools 在每次 run 入口执行）。
    """
    src = (_BACKEND / "app" / "tools" / "mcp_client.py").read_text(encoding="utf-8")
    start = src.index("async def sync_user_mcp_servers(")
    end = src.index("async def ", start + 10) if "async def " in src[start + 10:] else len(src)
    body = src[start:end]
    host_pos = body.find("_user_mcp_host_blocked(")
    lock_pos = body.find("async with _reload_lock_for_current_loop()")
    assert host_pos != -1, "未找到运行期私网判定"
    assert lock_pos != -1, "未找到 reload 锁"
    assert host_pos < lock_pos, (
        "私网判定（含 DNS）出现在进程级 reload 锁之后——会把 DNS 延迟放大成"
        "跨用户 head-of-line 阻塞"
    )


# --------------------------------------------------------------------------
# C4 — terminal save must not wipe existing tool_calls with None
# --------------------------------------------------------------------------

class _Bridge:
    def __init__(self, s):
        self._s = s

    async def execute(self, stmt, *a, **k):
        return self._s.execute(stmt, *a, **k)

    async def commit(self):
        self._s.commit()

    async def rollback(self):
        self._s.rollback()

    async def delete(self, obj):
        self._s.delete(obj)

    def add(self, obj):
        self._s.add(obj)

    async def flush(self):
        self._s.flush()

    async def refresh(self, obj):
        self._s.refresh(obj)


def _env():
    from app.db.database import (
        Base, ChatSession, Conversation, ConversationGroup, Message, MessagePayload, User,
    )
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @sa_event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables, stack, seen = [], [
        ConversationGroup.__table__, Conversation.__table__, Message.__table__,
        MessagePayload.__table__, ChatSession.__table__,
    ], set()
    while stack:
        t = stack.pop()
        if t.name in seen:
            continue
        seen.add(t.name)
        tables.append(t)
        for col in t.columns:
            for fk in col.foreign_keys:
                stack.append(fk.column.table)
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as s:
        s.add(User(id="u1", username="user1", password_hash="x"))
        s.add(Conversation(id="c1", user_id="u1", title="t"))
        s.commit()
    return engine


def _add_msg(engine, mid, *, content="", tool_calls=None, tool_results=None,
             reasoning=None, status="streaming", role="assistant", minutes=0):
    from app.db.database import Message
    with Session(engine) as s:
        s.add(Message(id=mid, conversation_id="c1", role=role, content=content,
                      reasoning_content=reasoning, tool_calls=tool_calls,
                      tool_results=tool_results, delivery_status=status,
                      created_at=_BASE + timedelta(minutes=minutes)))
        s.commit()


def _rows(engine):
    from app.db.database import Message
    with Session(engine) as s:
        return {m.id: m for m in s.execute(select(Message)).scalars()}


def test_c4_none_keeps_existing_tool_calls():
    """C4: tool_calls=None 表示「本次没带」，不是「清空」。"""
    from app.api.chat_persistence import _save_assistant_terminal

    engine = _env()
    calls = json.dumps([{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}])
    _add_msg(engine, "s1", content="", tool_calls=calls)

    async def go():
        with Session(engine) as s:
            await _save_assistant_terminal(
                _Bridge(s), conversation_id="c1", content="", reasoning=None,
                tool_results=None, tool_calls=None, context_info=None,
                status="final", row_id="s1",
            )
            s.commit()

    _run(go())
    assert _rows(engine)["s1"].tool_calls == calls, "stop/legacy-resume 不得抹掉 W11 工具证据"


def test_c4_real_value_still_replaces():
    """C4 反向约束：显式给定 tool_calls 时仍按新值覆盖。"""
    from app.api.chat_persistence import _save_assistant_terminal

    engine = _env()
    old = json.dumps([{"id": "old"}])
    new = json.dumps([{"id": "new"}])
    _add_msg(engine, "s1", content="", tool_calls=old)

    async def go():
        with Session(engine) as s:
            await _save_assistant_terminal(
                _Bridge(s), conversation_id="c1", content="x", reasoning=None,
                tool_results=None, tool_calls=new, context_info=None,
                status="final", row_id="s1",
            )
            s.commit()

    _run(go())
    assert _rows(engine)["s1"].tool_calls == new


# --------------------------------------------------------------------------
# C5 — single evidence predicate; no silent deletion of tool evidence
# --------------------------------------------------------------------------

def test_c5_predicate_reasoning_only_is_not_persistable():
    """C5: reasoning-only 仍不落 final（W5 ⛔ 裁定保持），但崩溃 sweep 仍视其为证据。"""
    from app.api.chat_persistence import _has_turn_evidence

    row = SimpleNamespace(content="", reasoning_content="思考", tool_calls=None, tool_results=None)
    assert _has_turn_evidence(row) is True, "崩溃 sweep：reasoning 是证据，保留为 interrupted"
    assert _has_turn_evidence(row, include_reasoning=False) is False, (
        "主动 stop：reasoning-only 不落 final（W5 D-20261031-msgdel-p1-semantics）"
    )


def test_c5_tool_calls_is_evidence():
    """C5: 有工具调用、无正文的行必须算证据（旧判据把它当空壳删掉）。"""
    from app.api.chat_persistence import _has_turn_evidence

    row = SimpleNamespace(content="", reasoning_content=None,
                          tool_calls='[{"id":"c1"}]', tool_results=None)
    assert _has_turn_evidence(row) is True
    assert _has_turn_evidence(row, include_reasoning=False) is True


def test_c5_delete_empty_streaming_row_keeps_tool_evidence():
    """C5: stop 路径的空行清理不得删除带工具证据的行（与 boot sweep 一致）。"""
    from app.api.chat_persistence import _delete_empty_streaming_row

    engine = _env()
    calls = json.dumps([{"id": "call_1"}])
    _add_msg(engine, "s1", content="", tool_calls=calls)

    async def go():
        with Session(engine) as s:
            return await _delete_empty_streaming_row(_Bridge(s), "s1")

    deleted = _run(go())
    assert deleted is False, "带工具证据的行不得被 stop 路径删除"
    assert "s1" in _rows(engine)


def test_c5_delete_empty_streaming_row_removes_true_shell():
    """C5 反向约束：真正的空壳（含 whitespace-only）仍要被清掉。"""
    from app.api.chat_persistence import _delete_empty_streaming_row

    engine = _env()
    _add_msg(engine, "s2", content="   ")

    async def go():
        with Session(engine) as s:
            res = await _delete_empty_streaming_row(_Bridge(s), "s2")
            s.commit()
            return res

    assert _run(go()) is True
    assert "s2" not in _rows(engine)


def test_c5_tool_calls_only_stop_persists_and_does_not_strand():
    """C5（行为面）：只有工具调用、正文为空的 stop 必须**落库**且**不留 streaming**。

    覆盖评审 I2/I3：门槛计了 tool_calls 但复检不计 → 翻回 False → 行走空行清理
    → 要么被删（证据丢）、要么留在 streaming（重启后才变空气泡）。这里直接跑
    真实 stop 处理器并检查落库终态。
    """
    engine = _env()
    _add_msg(engine, "s1", content="")          # 在途行：无正文
    calls = json.dumps([{"id": "call_1", "type": "function",
                         "function": {"name": "terminal", "arguments": "{}"}}])
    from app.db.database import Message
    with Session(engine) as ses:
        row = ses.get(Message, "s1")
        row.tool_calls = calls
        ses.commit()

    from tests.test_stop_reasoning_only_p1 import _mk_run, _patch, _run as _run2  # type: ignore
    cts = _patch(__import__("pytest").MonkeyPatch(), engine)
    run = _mk_run(content="", reasoning="", tools=[], row_id="s1")
    run.tool_call_events_accumulated = [{"call_id": "call_1", "name": "terminal",
                                         "arguments": {}}]
    _run2(cts._handle_agent_loop_cancel(run))

    row = _rows(engine)["s1"]
    assert row.delivery_status != "streaming", (
        "只有工具调用的 stop 不得把行留在 streaming（I2 搁浅）"
    )
    assert row.content == "" and row.tool_calls, "工具证据必须保留"


def test_c5_evidence_row_settles_to_interrupted_not_streaming():
    """C5（行为面，I2）：空行清理遇到有证据的行 → 当场翻 interrupted，不留 streaming。"""
    from app.api.chat_persistence import _delete_empty_streaming_row

    engine = _env()
    _add_msg(engine, "s1", content="", tool_calls=json.dumps([{"id": "c1"}]))

    async def go():
        with Session(engine) as ses:
            res = await _delete_empty_streaming_row(_Bridge(ses), "s1")
            ses.commit()
            return res

    assert _run(go()) is False
    assert _rows(engine)["s1"].delivery_status == "interrupted", (
        "有证据的行必须当场收口为 interrupted（否则重启前一直不可见）"
    )


def test_c5_no_conversation_scoped_streaming_bulk_delete():
    """C5: 不得存在「按会话批量删 content 为空的 streaming 行」的判据。

    该判据会误删并发 run 的证据行；必须全部改为按本 run 行 id 精确收口。
    """
    for name in ("chat_stream_transport.py", "chat.py", "chat_persistence.py"):
        src = (_BACKEND / "app" / "api" / name).read_text(encoding="utf-8")
        assert '(Message.content == "") | (Message.content.is_(None))' not in src, (
            f"{name} 仍存在 content-only 的 streaming 删除判据"
        )


def test_c1_stop_row_feeds_back_into_model_history():
    """C1（评审 Critical）：stop 落库的行必须能被 rebuild_structured_history 消费。

    旧实现把原始事件形状 `{call_id,name,arguments}` 写进 tool_calls、把
    `tool_results` 写成 list；下一轮 rebuild 会 `data.get(...)` 一个 list →
    AttributeError 抛在 agent loop 的 try 之外 → 整轮 run 死亡。
    """
    from app.services.tool_history import (
        build_persisted_tool_calls, rebuild_structured_history,
    )

    # 原始事件形状 → 规范形状
    raw_events = [{"call_id": "call_1", "name": "terminal", "arguments": {"cmd": "ls"}}]
    canonical = build_persisted_tool_calls(raw_events)
    assert canonical and json.loads(canonical)[0]["function"]["name"] == "terminal"

    # list 形状的 tool_results（W11 渐进落库 / stop 分支历史写入形态）
    list_results = json.dumps([{"call_id": "call_1", "name": "terminal",
                                "result": "file.txt"}])
    msgs = rebuild_structured_history("assistant", "部分回答", canonical, list_results)
    assert len(msgs) == 2, "必须产出 assistant + tool 两条"
    assert msgs[1]["role"] == "tool" and msgs[1]["tool_call_id"] == "call_1"
    assert msgs[1]["content"] == "file.txt", "必须读到真实结果（不是 orphan 桩）"

    # 即便 tool_calls 仍是原始形状，也必须被归一化而不是发出非法条目
    msgs2 = rebuild_structured_history(
        "assistant", "", json.dumps(raw_events), list_results
    )
    assert msgs2[0]["tool_calls"][0]["type"] == "function"
    assert msgs2[1]["content"] == "file.txt"


# --------------------------------------------------------------------------
# C6 — every terminal save binds row_id
# --------------------------------------------------------------------------

def test_c6_all_terminal_saves_bind_row_id():
    """C6: 7/7 个 _save_assistant_terminal 调用点都必须传 row_id。"""
    files = [_BACKEND / "app" / "api" / "chat_stream_transport.py",
             _BACKEND / "app" / "api" / "chat.py"]
    missing = []
    total = 0
    for f in files:
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"_save_assistant_terminal\(", src):
            total += 1
            tail = src[m.end(): m.end() + 1200]
            # 调用实参到下一个 ") " 行为止的近似窗口
            if "row_id=" not in tail:
                missing.append(f"{f.name}:{src[:m.start()].count(chr(10)) + 1}")
    assert total == 7, f"调用点数变化（{total}），需重新核对 row_id 覆盖"
    assert not missing, "以下调用点未绑定 row_id，会回退「最新 streaming 行」反查: " + repr(missing)


# --------------------------------------------------------------------------
# C7 — release strip must exclude the repo-root runtime jobs dir
# --------------------------------------------------------------------------

def test_c7_strip_excludes_root_output_files():
    """C7: 根级 output_files/ 必须进 strip 排除清单（否则 run.sh 命中 FORBID → exit 2）。"""
    src = (_REPO / "tools" / "opensource_strip.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    deny = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DENY_DIRS":
                    deny = ast.literal_eval(node.value)
    assert deny is not None, "未找到 DENY_DIRS"
    assert "output_files" in deny, "根级 output_files 必须列入 DENY_DIRS"


def test_c7_gitignore_covers_root_output_files():
    """C7: 根级 output_files/ 必须被 gitignore（当前只忽略 backend/ 作用域）。"""
    lines = (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    entries = {l.strip().rstrip("/") for l in lines if l.strip() and not l.strip().startswith("#")}
    assert "output_files" in entries, "根级 output_files 未进 .gitignore"
