"""bulk-export 端点回归（UPSTREAM_TODO_20261006 项 7，2026-10-07 波）。

契约：POST /notes/notes/bulk-export 必须接受 endnote_enabled（镜像
export_note 查询参数模式，backend/app/api/notes.py:2791）——旧代码引用未定义名，
key_parts 无条件求值连 md 路径一并 500（评审 A Critical-1）。本测试真调端点函数
（monkeypatch 渲染与去重入口），旧代码必 NameError（RED），修复后返回
StreamingResponse（zip）（GREEN）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_bulk_export_endpoint.py -v（vendor 布局）
"""
import asyncio
import inspect
import os
import sys
import zipfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_signature_carries_endnote_enabled():
    from app.api.notes import bulk_export_notes

    params = inspect.signature(bulk_export_notes).parameters
    assert "endnote_enabled" in params, "bulk-export 必须接受 endnote_enabled（镜像 export_note）"
    assert params["endnote_enabled"].default is True


class _FakeResult:
    def __init__(self, notes):
        self._notes = notes

    def scalars(self):
        return SimpleNamespace(all=lambda: self._notes)


class _FakeDB:
    async def execute(self, stmt):
        note = SimpleNamespace(id="n1", title="t", updated_at="2026-01-01")
        return _FakeResult([note])


@pytest.mark.parametrize("fmt", ["md", "pdf"])
def test_bulk_export_runs_with_endnote_param(fmt, tmp_path, monkeypatch):
    from app.api import notes as notes_api
    from app.services import export_registry

    async def fake_ws(db, uid):
        return str(tmp_path)

    monkeypatch.setattr(notes_api, "_get_note_workspace_root", fake_ws)
    monkeypatch.setattr(notes_api, "_build_note_md", lambda note, ws: "md-content")

    render_ee = []

    def _fake_render(note, ws, ee=True):
        render_ee.append(ee)
        return b"%PDF-1.4"

    monkeypatch.setattr(notes_api, "_render_note_pdf", _fake_render)
    monkeypatch.setattr(notes_api, "_read_export_file", lambda p: open(p, "rb").read())

    out_dir = tmp_path
    seen_key_parts = []

    async def fake_dedup(db, uid, **kw):
        seen_key_parts.append(kw["key_parts"])
        data = kw["render_fn"]()
        p = os.path.join(out_dir, "notes_export.zip")
        with open(p, "wb") as f:
            f.write(data)
        return p, "notes_export.zip", False

    monkeypatch.setattr(export_registry, "render_with_dedup", fake_dedup)

    resp = asyncio.run(notes_api.bulk_export_notes(
        export_data=SimpleNamespace(note_ids=["n1"], format=fmt),
        endnote_enabled=False,
        db=_FakeDB(),
        current_user=SimpleNamespace(id="u1"),
    ))
    assert resp is not None
    with zipfile.ZipFile(os.path.join(out_dir, "notes_export.zip")) as zf:
        assert zf.namelist() == [f"t.{fmt}"]
    # 评审 A-3（fix-round 1）：参数必须真正穿线到渲染与缓存键（防 accept-but-ignore）
    # 挂账清零波 T3 契约修订：ee 仅 pdf 渲染输入——pdf 键含参、md 键恒等（不含）。
    if fmt == "pdf":
        assert seen_key_parts and str(False) in seen_key_parts[0], (
            f"pdf 键必须含 endnote_enabled，got {seen_key_parts}")
        assert render_ee == [False], "endnote_enabled=False 必须传到 _render_note_pdf"
    else:
        assert seen_key_parts and str(False) not in seen_key_parts[0], (
            f"md 键不得按 ee 分叉（T3 契约），got {seen_key_parts}")
