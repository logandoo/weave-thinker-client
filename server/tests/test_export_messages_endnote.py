"""对话记录导出 endnote 穿线（挂账清零波 T4 / criterion 4）。

契约：POST /conversations/export-pdf 请求体带 `endnote_enabled`（默认 True——
未指定调用方兼容），穿线到 (a) 渲染函数（`_markdown_to_html_with_mermaid` 的
尾注开关）与 (b) 缓存键（单条 key_parts / 批量 zip_key——指纹覆盖渲染输入全集）。

Run: cd backend && ../.venv/bin/python -m pytest tests/test_export_messages_endnote.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _run(coro):
    return asyncio.run(coro)


def _mk_env(monkeypatch, tmp_path):
    from app.api import conversation as conv_api
    from app.services import export_registry as er

    seen = {"keys": None, "ee": []}

    async def fake_ws(db, uid):
        return None

    def fake_render(title, content, created_at="", workspace_root=None, endnote_enabled=True):
        seen["ee"].append(endnote_enabled)
        return b"%PDF-x"

    async def fake_dedup(db, uid, **kw):
        seen["keys"] = kw["key_parts"]
        data = kw["render_fn"]()
        p = os.path.join(str(tmp_path), "out.bin")
        with open(p, "wb") as f:
            f.write(data if isinstance(data, (bytes, bytearray)) else b"")
        return p, "out.bin", False

    monkeypatch.setattr(conv_api, "_get_note_workspace_root", fake_ws)
    monkeypatch.setattr(conv_api, "_render_messages_pdf", fake_render)
    monkeypatch.setattr(er, "render_with_dedup", fake_dedup)
    return conv_api, seen


def test_export_messages_single_threads_endnote(tmp_path, monkeypatch):
    conv_api, seen = _mk_env(monkeypatch, tmp_path)
    payload = SimpleNamespace(items=[SimpleNamespace(title="用户", content="正文", role="user")],
                              action="single", endnote_enabled=False)
    _run(conv_api.export_messages_pdf(payload, db=SimpleNamespace(),
                                      current_user=SimpleNamespace(id="u1")))
    assert seen["ee"] == [False], f"渲染必须收 endnote_enabled=False，got {seen['ee']}"
    assert str(False) in seen["keys"], f"单条键必须含 endnote_enabled，got {seen['keys']}"


def test_export_messages_bulk_threads_endnote(tmp_path, monkeypatch):
    conv_api, seen = _mk_env(monkeypatch, tmp_path)
    payload = SimpleNamespace(
        items=[SimpleNamespace(title="用户", content="一", role="user"),
               SimpleNamespace(title="助手", content="二", role="assistant")],
        action="bulk", endnote_enabled=False)
    _run(conv_api.export_messages_pdf(payload, db=SimpleNamespace(),
                                      current_user=SimpleNamespace(id="u1")))
    assert seen["ee"] == [False, False], f"批量逐条渲染必须收 False，got {seen['ee']}"
    assert str(False) in seen["keys"], f"zip 键必须含 endnote_enabled，got {seen['keys']}"


def test_export_messages_default_true_compat(tmp_path, monkeypatch):
    """未指定 → 默认 True（旧调用方兼容）。"""
    conv_api, seen = _mk_env(monkeypatch, tmp_path)
    from app.api.conversation import ExportMessagesPDFBulkRequest, ExportMessagesPDFRequest
    payload = ExportMessagesPDFBulkRequest(
        items=[ExportMessagesPDFRequest(title="用户", content="正文", role="user")],
        action="single")
    assert payload.endnote_enabled is True, "模型缺省必须 True（旧调用方兼容）"
    _run(conv_api.export_messages_pdf(payload, db=SimpleNamespace(),
                                      current_user=SimpleNamespace(id="u1")))
    assert seen["ee"] == [True]
