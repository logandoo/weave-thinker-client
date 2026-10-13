"""RED tests (A4.8) — pdf_export export_file 悬空 import（2026-09-26）。

生产事故（60, conv ef4bddbb）：942a2319b 安全波把 provide_file 的路径解析
迁移到 app.services.workspace_paths 后，pdf_exporter.py 仍 import 已删除的
``provide_file._resolve_within_workspace`` → 每次 export_file 都 ImportError，
被 pdf_export 顶层 except 包装为 ``{"error": "PDF export failed: cannot import
name ..."}``。export_file 路径此前无任何测试覆盖，故存活至生产。

契约（pdf_exporter._resolve_workspace_file docstring）：
  - 界内绝对路径 / 工作区相对路径 / 裸文件名递归搜索 → 解析为绝对路径字符串；
  - 界外绝对路径 / ``..`` 穿越 / 不存在 → None；
  - export_file 端到端：工作区 markdown → 真实 PDF 落盘（%PDF magic + 非空）。

Run: cd backend && python -m pytest tests/test_pdf_export_resolve.py -v
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools import pdf_exporter  # noqa: E402


@pytest.fixture()
def workspace(tmp_path):
    root = tmp_path / "ws"
    (root / "charts").mkdir(parents=True)
    (root / "guide.md").write_text("# 合规指南\n\n第一章 适用定位。\n", encoding="utf-8")
    (root / "charts" / "report.md").write_text("# 报告\n\n正文。\n", encoding="utf-8")
    return root


def test_resolve_absolute_inside(workspace):
    target = str(workspace / "guide.md")
    assert pdf_exporter._resolve_workspace_file(target, str(workspace)) == str(
        workspace / "guide.md"
    )


def test_resolve_relative(workspace):
    assert pdf_exporter._resolve_workspace_file("guide.md", str(workspace)) == str(
        workspace / "guide.md"
    )


def test_resolve_bare_name_recursive(workspace):
    assert pdf_exporter._resolve_workspace_file("report.md", str(workspace)) == str(
        workspace / "charts" / "report.md"
    )


def test_resolve_outside_absolute_returns_none(workspace, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    assert pdf_exporter._resolve_workspace_file(str(outside), str(workspace)) is None


def test_resolve_traversal_returns_none(workspace, tmp_path):
    # 真正逃逸工作区的 `..`（解析后落在 root 之外）必须拒绝；
    # `../ws/...` 这类解析后仍回界内的属于无害路径（containment 契约）。
    (tmp_path / "sibling.md").write_text("x", encoding="utf-8")
    assert pdf_exporter._resolve_workspace_file("../sibling.md", str(workspace)) is None


def test_resolve_missing_returns_none(workspace):
    assert pdf_exporter._resolve_workspace_file("nope.md", str(workspace)) is None


def test_export_file_end_to_end(workspace, monkeypatch):
    """生产事故路径：agent workspace_write 产出 md 后 pdf_export export_file。"""
    import asyncio

    async def fake_workspace_root(db, user):
        return str(workspace)

    async def run():
        monkeypatch.setattr(pdf_exporter, "_get_workspace_root", fake_workspace_root)
        user = SimpleNamespace(id="u-test", username="test")
        raw = await pdf_exporter.pdf_export(
            {"action": "export_file", "file_path": "guide.md"},
            user=user,
            db=SimpleNamespace(),  # 不到达 DB：_get_workspace_root 已替换
        )
        return json.loads(raw)

    data = asyncio.run(run())
    assert "error" not in data, data
    assert data["success"] is True
    out = workspace / "pdf_exports" / data["filename"]
    assert out.is_file()
    head = out.read_bytes()[:5]
    assert head == b"%PDF-"
    assert data["size"] > 500
    # 交付载荷（评审 I-4：生产端 pin——generated_files 只带 rel_path，绝不带系统绝对路径）
    gf = data.get("generated_files") or []
    assert len(gf) == 1, data
    assert gf[0]["name"] == data["filename"]
    assert gf[0]["rel_path"] == f"pdf_exports/{data['filename']}"
    assert gf[0]["type"] == "pdf"
    assert not str(gf[0].get("path", "")).startswith("/"), gf[0]


def test_export_note_reports_generated_files_rel_path(workspace, monkeypatch):
    """export_note 生产端 pin（conv b8dcedc8 场景）：真 pdf_export() 返回交付载荷。"""
    import asyncio

    async def fake_workspace_root(db, user):
        return str(workspace)

    async def fake_load_note(db, note_id, user_id):
        return SimpleNamespace(
            id=note_id, title="sample note",
            content="# T\n\nbody MARK_BODY.\n",
            created_at=None,
        )

    async def run():
        monkeypatch.setattr(pdf_exporter, "_get_workspace_root", fake_workspace_root)
        monkeypatch.setattr(pdf_exporter, "_load_note", fake_load_note)
        user = SimpleNamespace(id="u-test", username="test")
        raw = await pdf_exporter.pdf_export(
            {"action": "export_note", "note_id": "11111111-2222-3333-4444-555555555555"},
            user=user,
            db=SimpleNamespace(),
        )
        return json.loads(raw)

    data = asyncio.run(run())
    assert "error" not in data, data
    assert data["success"] is True
    gf = data.get("generated_files") or []
    assert len(gf) == 1, data
    assert gf[0]["rel_path"] == f"pdf_exports/{data['filename']}"
    assert gf[0]["size"] == data["size"] > 500
    assert not str(gf[0].get("path", "")).startswith("/"), gf[0]


def test_export_conversation_reports_generated_files_rel_path(workspace, monkeypatch):
    """export_conversation 分支 pin（复审残缺收口）：真 pdf_export() 返回交付载荷。"""
    import asyncio

    async def fake_workspace_root(db, user):
        return str(workspace)

    async def fake_load_conversation(db, cid, user_id):
        return SimpleNamespace(id=cid, title="sample conv")

    async def fake_load_messages(db, cid):
        return [SimpleNamespace(role="user", content="hi", tool_results=None)]

    async def run():
        monkeypatch.setattr(pdf_exporter, "_get_workspace_root", fake_workspace_root)
        monkeypatch.setattr(pdf_exporter, "_load_conversation", fake_load_conversation)
        monkeypatch.setattr(pdf_exporter, "_load_messages", fake_load_messages)
        user = SimpleNamespace(id="u-test", username="test")
        raw = await pdf_exporter.pdf_export(
            {"action": "export_conversation", "conversation_id": "99999999-8888-7777-6666-555555555555"},
            user=user,
            db=SimpleNamespace(),
        )
        return json.loads(raw)

    data = asyncio.run(run())
    assert "error" not in data, data
    assert data["success"] is True
    gf = data.get("generated_files") or []
    assert len(gf) == 1, data
    assert gf[0]["rel_path"] == f"pdf_exports/{data['filename']}"
    assert not str(gf[0].get("path", "")).startswith("/"), gf[0]
