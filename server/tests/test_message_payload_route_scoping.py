"""message_payloads 路由寻址守卫（UPSTREAM_TODO_20261004 项2，P1 安全）。

契约（与 message_payload_service._lookup_payload 的 C2 归属寻址同款）：
  GET /api/messages/{message_id}/payload/{field} 只允许按 **(message_id, field)**
  归属取回 payload 行——全局 sha256 寻址会让「伪造桩」（自己消息的 stub 携带
  他消息的 payload_ref）越权读到非本消息的外置内容（跨消息/跨用户内容替换面）。
  行缺失（孤儿桩）→ 404，绝不把桩体本身当内容回吐（前端把 200 当成功 →
  静默停在预览，违背「截断必须可感知」红线）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_message_payload_route_scoping.py -v（vendor 布局）
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

FOREIGN_TEXT = "FOREIGN-SECRET-" + "x" * 200
OWN_TEXT = "own-" + "y" * 200


def _sha(s: str) -> str:
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()


class _Result:
    def __init__(self, scalar=None, row=None):
        self._scalar = scalar
        self._row = row

    def scalar_one_or_none(self):
        return self._scalar

    def first(self):
        return self._row


class _ScopedPayloadDB:
    """内存 payload 表（行按 (message_id, field, sha256, content) 归属）。

    execute() 按语句 WHERE 面返回行：
      - 仅 sha256 寻址 → 全表按 sha 命中（模拟全局寻址的越权面）
      - message_id+field 归属寻址 → 仅本消息行命中
    """

    def __init__(self, rows):
        self.rows = rows  # list of dict(message_id, field, sha256, content)

    async def execute(self, stmt):
        where = str(stmt)
        compiled = stmt.compile()
        p = dict(compiled.params)
        if "message_id" in where and "field" in where:
            mid = p.get("message_id_1") or p.get("message_id")
            fld = p.get("field_1") or p.get("field")
            for r in self.rows:
                if r["message_id"] == mid and r["field"] == fld:
                    return _Result(row=(r["content"],))
            return _Result(row=None)
        if "sha256" in where:
            ref = p.get("sha256_1") or p.get("sha256")
            for r in self.rows:
                if r["sha256"] == ref:
                    return _Result(row=(r["content"],))
            return _Result(row=None)
        raise AssertionError(f"unexpected statement shape: {where}")


def _msg(mid, conv_id, field, stored):
    return SimpleNamespace(id=mid, conversation_id=conv_id, **{field: stored})


def _run(coro):
    # asyncio.run 每次新建事件环——不依赖 ambient loop 状态（py3.13 下前序
    # async 测试清环后 get_event_loop() 会抛 RuntimeError，协程被丢弃）
    return asyncio.run(coro)


@pytest.fixture()
def env():
    """userB 的 payload 行 + userA 的消息（伪造桩指向 B 的 sha）。"""
    foreign_sha = _sha(FOREIGN_TEXT)
    rows = [dict(message_id="msg-B", field="tool_results",
                 sha256=foreign_sha, content=FOREIGN_TEXT)]
    user_a = SimpleNamespace(id="userA")
    user_b = SimpleNamespace(id="userB")

    state = {"stub_ref": foreign_sha}  # 桩 ref 显式可控（完整性腿须钉住原值）

    def get_msg(stmt):
        mid = dict(stmt.compile().params).get("id_1")
        if mid == "msg-A":
            return _Result(scalar=_msg("msg-A", "conv-A", "tool_results",
                                       json.dumps({"_externalized": True, "payload_ref": state["stub_ref"]})))
        if mid == "msg-B":
            return _Result(scalar=_msg("msg-B", "conv-B", "tool_results",
                                       json.dumps({"_externalized": True, "payload_ref": foreign_sha})))
        return _Result(scalar=None)

    def get_conv(stmt):
        cid = dict(stmt.compile().params).get("id_1")
        owner = {"conv-A": user_a, "conv-B": user_b}.get(cid)
        return _Result(scalar=SimpleNamespace(id=cid, user_id=owner.id) if owner else None)

    class _DB(_ScopedPayloadDB):
        async def execute(self, stmt):
            s = str(stmt)
            if "conversations" in s:
                return get_conv(stmt)
            if "message_payloads" in s:
                return await super().execute(stmt)
            return get_msg(stmt)

    env = SimpleNamespace(db=_DB(rows), user_a=user_a, user_b=user_b,
                          foreign_sha=foreign_sha)

    def _set_stub_ref(ref):
        state["stub_ref"] = ref
    env.set_stub_ref = _set_stub_ref
    return env


def test_forged_stub_cannot_read_foreign_payload(env):
    """伪造桩（本消息 stub 携带他消息 payload_ref）不得越权读到他消息内容。"""
    from fastapi import HTTPException

    from app.api.message_payloads import get_message_payload

    with pytest.raises(HTTPException) as ei:
        _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert ei.value.status_code == 404, (
        "伪造桩必须 404（按 (message_id, field) 归属寻址）——当前实现按全局 sha 回吐他消息内容"
    )


def test_forged_stub_response_never_contains_foreign_text(env):
    """双保险：任何返回形态都不得包含他消息外置内容。"""
    from fastapi import HTTPException

    from app.api.message_payloads import get_message_payload

    try:
        resp = _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    except HTTPException:
        return
    assert FOREIGN_TEXT not in json.dumps(resp), "他消息内容泄漏"


def test_orphan_stub_is_404_not_stub_content(env):
    """孤儿桩（行缺失）→ 404；不得把桩体当内容回吐（前端 200=成功 → 静默停在预览）。"""
    from fastapi import HTTPException

    from app.api.message_payloads import get_message_payload

    env.db.rows = []  # payload 行缺失
    with pytest.raises(HTTPException) as ei:
        _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert ei.value.status_code == 404


def test_owner_can_still_reach_own_payload(env):
    """守卫不误伤：本消息归属行照常取回（红→绿的非回归腿）。"""
    from app.api.message_payloads import get_message_payload

    env.set_stub_ref(_sha(OWN_TEXT))
    env.db.rows = [dict(message_id="msg-A", field="tool_results",
                        sha256=_sha(OWN_TEXT), content=OWN_TEXT)]
    resp = _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert resp["content"] == OWN_TEXT
    assert resp["externalized"] is True


def test_payload_integrity_mismatch_is_404(env):
    """minor⑦：payload 行内容与 stub.payload_ref 的 sha256 不符 → 404（完整性元数据落地）。"""
    from fastapi import HTTPException

    from app.api.message_payloads import get_message_payload

    # 归属行存在但内容被篡改（sha 对不上 stub 的 payload_ref）
    env.set_stub_ref(_sha(OWN_TEXT))
    env.db.rows = [dict(message_id="msg-A", field="tool_results",
                        sha256=_sha("TAMPERED-" + "z" * 100), content="TAMPERED-" + "z" * 100)]
    with pytest.raises(HTTPException) as ei:
        _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert ei.value.status_code == 404


def test_size_bytes_is_utf8_byte_count(env):
    """minor④：路由 size_bytes 与 build_field_stub 同语义=UTF-8 字节数（非字符数）。"""
    from app.api.message_payloads import get_message_payload

    text = "汉字三"  # 3 码位 / 9 字节
    env.set_stub_ref(_sha(text))
    env.db.rows = [dict(message_id="msg-A", field="tool_results",
                        sha256=_sha(text), content=text)]
    resp = _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert resp["content"] == text
    assert resp["size_bytes"] == 9, f"应为 UTF-8 字节数 9，实得 {resp['size_bytes']}"


def test_backfill_dup_row_is_route_fetchable(env):
    """W3 项2 链级：跨消息 dup 内容 → backfill 产出本消息行 → 归属路由 200。

    旧契约（skipped_dup + p=None）让 stub.payload_ref 指向他消息行 → 归属寻址
    404 孤儿；新契约 dup 也返回本消息 payload tuple。此腿串起：
    process_row(dup) → 本消息行 → get_message_payload 归属取回。
    """
    from scripts.backfill_message_payloads import process_row
    from app.services.message_payload_service import build_field_stub, compute_sha256

    fat = "dup-" + "q" * 5000
    sha = compute_sha256(fat)
    stub, payload, status = process_row(
        "tool_results", fat, {sha}, 1000, build_field_stub, compute_sha256)
    assert status == "duplicated" and payload is not None, "dup 必须产出本消息 payload tuple"
    assert payload[0] == "tool_results" and payload[1] == fat

    from app.api.message_payloads import get_message_payload

    # 真链：用 process_row 返回的 tuple 构造"调用方写入的本消息行"
    env.set_stub_ref(payload[3])
    env.db.rows = [dict(message_id="msg-A", field=payload[0], sha256=payload[3], content=payload[1])]
    resp = _run(get_message_payload("msg-A", "tool_results", db=env.db, current_user=env.user_a))
    assert resp["content"] == fat
    assert resp["size_bytes"] == len(fat.encode("utf-8"))
