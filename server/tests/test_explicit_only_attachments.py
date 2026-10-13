"""RED tests (A4.8) — 附件仅显式交付工具（用户指令，conv 8b382ad8 60 生产）。

背景：分析任务里 terminal 克隆/读取产生的文件被自动收集成下载卡片
（生产 conv 8b382ad8 落库 attachments=278 — `_refs/opencode`、`.github/**` 等
全部分析对象），违背 provide_file 工具「用户明确要求才提供文件」的初衷。
用户指令（2026-09-18）：未显式提供则一律不出卡片。

契约（2026-09-27 用户裁定 D-302 修订，prod conv b8dcedc8）：
  - `chat._collect_download_attachments` 收集显式交付工具 provide_file /
    provide_folder / **pdf_export** 成功调用的 `generated_files`；
    terminal/execute_code/code_execution 的副产品不进入附件集合；
  - pdf_export 交付用例已迁至 tests/test_pdf_export_delivery_attachment.py；
  - 类型推断（扩展名）与同名去重（保留最大 size）保持既有语义；
  - `agent_worker._extract_attachments`（后台任务）同策略。

Run: cd backend && python -m pytest tests/test_explicit_only_attachments.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.chat import _collect_download_attachments  # noqa: E402
from app.services.agent_worker import AgentWorker  # noqa: E402


def _tr(name, files):
    return {"name": name, "result": json.dumps({"generated_files": files})}


def _tr_pdf(filename, path, size=1024):
    return {
        "name": "pdf_export",
        "result": json.dumps(
            {"success": True, "filename": filename, "file_path": path, "size": size}
        ),
    }


def test_analysis_turn_without_provide_file_has_no_attachments():
    """验收 #1：terminal/execute_code/code_execution 产物一律不出卡片。

    （pdf_export 成功交付已按 D-302 出卡，见 test_pdf_export_delivery_attachment.py；
    此处保留一个无 generated_files 的旧式 pdf_export 结果作健壮性用例——不崩溃、不出卡。）
    """
    trs = [
        _tr("terminal", [
            {"name": "README.md", "path": "/ws/_refs/opencode/README.md", "size": 111},
            {"name": "apply_patch.rs", "path": "/ws/_refs/codex/apply_patch.rs", "size": 222},
        ]),
        _tr("execute_code", [
            {"name": "chart.png", "path": "/ws/chart.png", "size": 333},
        ]),
        _tr("code_execution", [
            {"name": "stats.txt", "path": "/ws/stats.txt", "size": 444},
        ]),
        _tr_pdf("report.pdf", "/ws/report.pdf", 555),
    ]
    assert _collect_download_attachments(trs) == []


def test_provide_file_set_wins_and_auto_products_never_mix():
    """验收 #2：provide_file 成功集合独占，terminal 产物不混入。"""
    trs = [
        _tr("terminal", [
            {"name": "analysis.py", "path": "/ws/analysis.py", "size": 999},
        ]),
        _tr("provide_file", [
            {"name": "final.docx", "path": "/ws/final.docx", "size": 10},
        ]),
    ]
    out = _collect_download_attachments(trs)
    assert [a["name"] for a in out] == ["final.docx"]


def test_provide_file_type_inference_and_name_dedup():
    """验收 #2 既有语义：扩展名类型推断 + 同名保留最大。"""
    trs = [
        _tr("provide_file", [
            {"name": "report.docx", "path": "/ws/report_v1.docx", "size": 10},
            {"name": "report.docx", "path": "/ws/report_v2.docx", "size": 42},
            {"name": "data.csv", "path": "/ws/data.csv", "size": 7, "type": "file"},
        ]),
    ]
    out = {a["name"]: a for a in _collect_download_attachments(trs)}
    assert out["report.docx"]["size"] == 42
    assert out["report.docx"]["type"] == "word"
    assert out["data.csv"]["type"] == "csv"


def test_worker_extract_attachments_explicit_only():
    """验收 #5：后台任务同策略。"""
    trs = [
        _tr("terminal", [
            {"name": "clone.md", "path": "/ws/_refs/readme.md", "size": 1},
        ]),
        _tr("execute_code", [
            {"name": "tmp.bin", "path": "/ws/tmp.bin", "size": 2},
        ]),
    ]
    assert AgentWorker._extract_attachments(object(), trs) == []


def test_worker_extract_attachments_provide_file_set():
    trs = [
        _tr("execute_code", [
            {"name": "noise.txt", "path": "/ws/noise.txt", "size": 3},
        ]),
        _tr("provide_file", [
            {"name": "deliver.xlsx", "path": "/ws/deliver.xlsx", "size": 4},
        ]),
    ]
    out = AgentWorker._extract_attachments(object(), trs)
    assert [a["name"] for a in out] == ["deliver.xlsx"]


def test_collector_tolerates_malformed_and_non_dict_entries():
    """健壮性：坏 JSON / 非 dict 元素 / 缺字段 / 重复 path 均不得崩溃或泄漏。"""
    trs = [
        {"name": "provide_file", "result": "not json"},
        _tr("provide_file", ["not-a-dict", None, {"path": "/ws/x"}]),
        _tr("provide_file", [
            {"name": "dup.txt", "path": "/ws/dup.txt", "size": 1},
            {"name": "dup.txt", "path": "/ws/dup.txt", "size": 9},
        ]),
    ]
    out = _collect_download_attachments(trs)
    assert [a["name"] for a in out] == ["dup.txt"]
    assert out[0]["size"] == 9


def test_worker_tolerates_malformed_entries():
    trs = [
        {"name": "provide_file", "result": "{bad json"},
        _tr("provide_file", ["x", {"path": ""}, {"name": "ok.txt", "path": "/ws/ok.txt"}]),
    ]
    out = AgentWorker._extract_attachments(object(), trs)
    assert [a["name"] for a in out] == ["ok.txt"]
