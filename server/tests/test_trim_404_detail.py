"""RED tests (A4.8) — trim 404 可区分 detail（2026-10-17 判据#2，conv 92398a80）。

生产事故：regenerate/edit 目标缺失时前后端只见「待重新生成的消息不存在 /
HTTP error: 404」，用户无法区分「目标真被删了」与「目标是 stop 残留的
streaming 空行」（后者刷新即自愈）。判据#2：两种情形返回可区分 detail；
普通追加路径永不 404（设计上无 404 源，源码钉死）。

契约：
  D1 `_trim_missing_detail(status, kind)` 纯函数：status=='streaming' →
     「中断未保存」文案；None → 既有「待重新生成/待编辑的消息不存在」。
  D2 两个 trim helper 接线：目标缺失时先 `_trim_target_status` 再按 D1 抛 404。
  D3 普通追加分支（regenerate/edit 之外的 else）源码不含 404 raise。

Run: cd backend && python -m pytest tests/test_trim_404_detail.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_d1_streaming_residue_detail():
    from app.api.chat import _trim_missing_detail
    d = _trim_missing_detail("streaming", "regenerate")
    assert "中断" in d and "刷新" in d, f"streaming 残留 detail 不含中断/刷新指引: {d}"


def test_d1b_absent_details_keep_legacy_text():
    from app.api.chat import _trim_missing_detail
    assert _trim_missing_detail(None, "regenerate") == "待重新生成的消息不存在"
    assert _trim_missing_detail(None, "edit") == "待编辑的消息不存在"
    # streaming 残留 × edit 分支也有可懂文案
    assert "刷新" in _trim_missing_detail("streaming", "edit")


def test_d2_both_trims_wire_distinguishable_404():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    src += open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat_stream_transport.py"),
                encoding="utf-8").read()
    for fn in ("_trim_messages_for_regeneration", "_trim_messages_for_edit"):
        start = src.index(f"async def {fn}")
        end = src.index("\nasync def ", start + 10)
        body = src[start:end]
        assert "_trim_target_status(" in body, f"{fn} 未做目标存在性二次查询"
        assert "_trim_missing_detail(" in body, f"{fn} 未接线可区分 detail"


def test_d3_plain_append_never_404s():
    """普通追加（else 分支：建新 user 消息）源码不含任何 404 raise。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    src += open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat_stream_transport.py"),
                encoding="utf-8").read()
    # else 分支锚：「else:\n        user_message = Message(」——区别于 edit 分支
    # 的同缩进同语句（其窗口会扫到编辑异常处理注释里的 404 字样）。
    idx = src.index("    else:\n        user_message = Message(")
    block = src[idx: idx + 1200]
    assert "404" not in block, "普通追加分支出现 404 —— 违反判据#2 永不 404 承诺"


def test_d4_target_status_scoped_to_conversation():
    """评审 M1（Security）：_trim_target_status 必须带 conversation_id 谓词——
    否则跨会话 1-bit 状态 oracle（streaming 残留文案 vs 不存在）。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat.py"),
               encoding="utf-8").read()
    src += open(os.path.join(os.path.dirname(__file__), "..", "app", "api", "chat_stream_transport.py"),
                encoding="utf-8").read()
    start = src.index("async def _trim_target_status")
    body = src[start: src.index("\nasync def ", start + 10)]
    assert "Message.conversation_id" in body, \
        "_trim_target_status 未按 conversation_id 收敛（跨会话状态 oracle）"
