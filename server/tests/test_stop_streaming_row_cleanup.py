"""RED tests (A4.8) — stop 零内容僵尸 streaming 行清理（conv 92398a80，2026-10-17）。

生产事故：用户在 run 产出首个 token 前点停止（Stop-cancel content=0 reasoning=0
segments=0 tools=0），chat.py 的 CancelledError 分支只在「有内容/工具/思考」时
落库（4169 守卫），否则直接跳过——run 开始时创建的 delivery_status='streaming'
空行（渐进落库 R2）无人删除，成为僵尸行（55d66144，10-16 16:38 CST 实证）。
4047 行的空行清道夫只覆盖「SSE 取消但 loop 已完成」分支，不覆盖 stop 分支。

契约：
  C1 `_delete_empty_streaming_row(db, row_id)`：按行 id 精确删除，守卫
     （id == row_id AND delivery_status == 'streaming' AND 无任何证据），返回是否删除；
     row_id 空 → False 且不触 DB。

  2026-11-02（C5 规格变更）：判据从「content 为空」改为与启动 sweep 同源的
  `_has_turn_evidence(row, include_reasoning=False)` —— 实现由「单条 SQL DELETE」
  改为「先按 id+streaming 取出，再判证据，再 ORM delete」。**行为契约不变且更强**：
  带 tool_calls/tool_results 证据的行现在也不会被删（旧 SQL 判据会删掉它们）。
  下面三条用例据此改写（- test-change 记录在 verification_log.md）。
  C2 非空 streaming 行/终态行绝不被删（守卫在 SQL 层，不靠调用方）。
  C3 接线：chat.py 的 agent-task `except asyncio.CancelledError` 分支在零内容
     不落库时调用该 helper（行 id 取 buffer 的 streaming_message_id）。

Run: cd backend && python -m pytest tests/test_stop_streaming_row_cleanup.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


def test_c1_deletes_only_the_empty_streaming_row():
    from app.api.chat import _delete_empty_streaming_row
    from app.db.database import Message

    row = SimpleNamespace(content="", reasoning_content=None,
                          tool_calls=None, tool_results=None)
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: row))
    )
    ok = _run(_delete_empty_streaming_row(db, "row-1"))
    assert ok is True
    db.delete.assert_awaited_once_with(row)

    stmt = db.execute.call_args[0][0]
    compiled = stmt.compile()
    sql = str(compiled)
    assert sql.lstrip().upper().startswith("SELECT"), f"必须先取出再判证据: {sql}"
    params = compiled.params
    # 行 id 精确 + streaming 在途守卫（参数层钉死，防改写成会话级横扫）
    assert "row-1" in params.values(), f"缺行 id 守卫: {params}"
    assert "streaming" in params.values(), f"缺 streaming 守卫: {params}"


def test_c1b_no_row_id_no_db_touch():
    from app.api.chat import _delete_empty_streaming_row

    db = AsyncMock()
    ok = _run(_delete_empty_streaming_row(db, None))
    assert ok is False
    db.execute.assert_not_called()


def test_c1c_zero_rowcount_reports_false():
    """行不存在（已被终态路径翻掉/删除）→ False，且不执行 delete。"""
    from app.api.chat import _delete_empty_streaming_row

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None))
    )
    assert _run(_delete_empty_streaming_row(db, "row-x")) is False
    db.delete.assert_not_awaited()


def test_c3_stop_cancel_wires_cleanup():
    """CancelledError 分支零内容时调用清理 helper（源码钉——防静默丢线）。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    src += open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat_stream_transport.py"),
                encoding="utf-8").read()
    # C-02c: the stop-cancel branch body moved verbatim into
    # `_handle_agent_loop_cancel` (called from the CancelledError handler);
    # pin that function directly instead of scanning every except-block.
    marker = "async def _handle_agent_loop_cancel("
    nxt = src.index(marker)
    # 2026-11-02：改为「从函数头向后取一个大窗口」——旧的固定 9000 字符切片
    # 会在函数体增长后切掉尾巴（本波在该分支新增了 tool_calls 判据与注释），
    # 失败与产品行为无关。断言本身不变：该分支必须接线空行清理。
    block = src[nxt: nxt + 16000]
    assert "Stop-cancel conv=" in block, "未定位到 agent-task 的 stop-cancel 分支"
    assert "_delete_empty_streaming_row(" in block, \
        "stop-cancel 分支未接线空 streaming 行清理（僵尸行会复发）"


def test_c4_caller_commits_the_delete():
    """评审 C1（2026-10-18）：helper 只 execute 不 commit——调用方必须提交，
    否则 async with 退出回滚、删除是空操作。钉调用点 commit 存在且在其后。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    src += open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat_stream_transport.py"),
                encoding="utf-8").read()
    # 锚定 stop-cancel 分支（同 test_c3：C-02c 后为 `_handle_agent_loop_cancel`）。
    marker = "async def _handle_agent_loop_cancel("
    nxt = src.index(marker)
    # 2026-11-02：改为「从调用点向后取窗口」——旧的 `block = src[nxt:nxt+9000]`
    # 会在函数体增长后把 commit 切掉（C5 在本分支新增了判据注释与
    # tool_calls 计算），失败与产品行为无关。断言本身不变：调用后必须 commit。
    call = src.index("_delete_empty_streaming_row(", nxt)
    window = src[call: call + 1500]
    assert "commit()" in window, "stop-cancel 清理调用后缺 commit——DELETE 会被回滚（C1）"
