"""RED tests (A4.8) — 附件收集支持 provide_folder 文件夹卡片（2026-09-19）。

契约（docs/BACKEND_DESIGN_workspace_preview_folders_20260919.html §4）：
  - `chat._collect_download_attachments` 同时收集 provide_file.generated_files
    与 provide_folder.generated_folders；folder 条目保持 type="folder"；
  - 文件条目仍按 name 去重（大 size 优先）；folder 条目按 rel_path 去重；
  - 类型推断只作用于文件条目（不得把 folder 误判为普通 file）；
  - `agent_worker._extract_attachments` 同规则（rel_path 或 legacy path 去重）。

Run: cd backend && python -m pytest tests/test_attachment_folder_collection.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.chat import _collect_download_attachments  # noqa: E402
from app.services.agent_worker import AgentWorker  # noqa: E402


def _tr(name, payload):
    return {"name": name, "result": json.dumps(payload)}


def test_collects_files_and_folders():
    trs = [
        _tr("provide_file", {"generated_files": [
            {"name": "report.docx", "rel_path": "files/report.docx", "size": 10, "type": "word"},
        ]}),
        _tr("provide_folder", {"generated_folders": [
            {"name": "output", "rel_path": "files/output", "size": 30, "file_count": 3, "type": "folder"},
        ]}),
    ]
    atts = _collect_download_attachments(trs)
    by_type = {a["type"]: a for a in atts}
    assert set(by_type) == {"word", "folder"}
    assert by_type["folder"]["rel_path"] == "files/output"
    assert by_type["folder"]["file_count"] == 3


def test_folder_type_not_inferred_to_file():
    trs = [_tr("provide_folder", {"generated_folders": [
        {"name": "noext", "rel_path": "files/noext", "size": 0, "file_count": 0},
    ]})]
    (att,) = _collect_download_attachments(trs)
    assert att["type"] == "folder"


def test_folders_dedupe_by_rel_path():
    trs = [
        _tr("provide_folder", {"generated_folders": [
            {"name": "output", "rel_path": "files/output", "size": 1, "file_count": 1, "type": "folder"},
        ]}),
        _tr("provide_folder", {"generated_folders": [
            {"name": "output", "rel_path": "files/output", "size": 99, "file_count": 9, "type": "folder"},
        ]}),
    ]
    atts = _collect_download_attachments(trs)
    assert len(atts) == 1
    assert atts[0]["file_count"] == 9  # largest wins, same as file dedupe semantics


def test_files_still_dedupe_by_name_largest_size():
    trs = [
        _tr("provide_file", {"generated_files": [
            {"name": "a.txt", "rel_path": "files/a_v1.txt", "size": 3, "type": "text"},
            {"name": "a.txt", "rel_path": "files/a_v2.txt", "size": 30, "type": "text"},
        ]}),
    ]
    (att,) = _collect_download_attachments(trs)
    assert att["rel_path"] == "files/a_v2.txt"


def test_no_provide_tools_no_attachments():
    trs = [_tr("terminal", {"generated_files": [{"name": "x", "path": "/ws/x"}]})]
    assert _collect_download_attachments(trs) == []


def test_worker_extract_includes_folders():
    trs = [
        _tr("provide_file", {"generated_files": [
            {"name": "a.txt", "rel_path": "files/a.txt", "size": 1, "type": "text"},
        ]}),
        _tr("provide_folder", {"generated_folders": [
            {"name": "out", "rel_path": "files/out", "size": 2, "file_count": 1, "type": "folder"},
        ]}),
    ]
    out = AgentWorker._extract_attachments(object(), trs)
    assert [a["type"] for a in out] == ["text", "folder"]


def test_worker_extract_legacy_absolute_path_still_dedupes():
    trs = [
        _tr("provide_file", {"generated_files": [
            {"name": "a.txt", "path": "/ws/a.txt", "size": 1, "type": "text"},
            {"name": "a.txt", "path": "/ws/a.txt", "size": 1, "type": "text"},
        ]}),
    ]
    out = AgentWorker._extract_attachments(object(), trs)
    assert len(out) == 1
