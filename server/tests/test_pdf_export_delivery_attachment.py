"""RED tests (A4.8) — pdf_export 显式交付产物出下载卡片（conv b8dcedc8，用户裁定 D-0/2026-09-27）。

背景（生产 conv b8dcedc8）：用户「我保存到了笔记……你导出为 pdf」→ agent 调
pdf_export.export_note 成功产出 龙潭站水位数据_97683e68.pdf（文件真实落盘），
但消息无下载卡片——_collect_download_attachments 只认 provide_file/provide_folder
（2026-09-18 裁定），pdf_export 结果被排除，模型也未补调 provide_file。

用户裁定（2026-09-27，D-302）：两者都做——
  ① pdf_export 成功导出=显式交付，产物出卡（terminal/execute_code/code_execution
     产物仍排除，2026-09-18 事故面不变）；
  ② 提示词/工具描述兜底要求导出后补调 provide_file（同名去重，不会重复出卡）。

Run: cd backend && python -m pytest tests/test_pdf_export_delivery_attachment.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api.chat import _collect_download_attachments  # noqa: E402
from app.services.agent_worker import AgentWorker  # noqa: E402


def _tr(name, files):
    return {"name": name, "result": json.dumps({"generated_files": files})}


def _tr_pdf_export(filename="龙潭站水位数据_97683e68.pdf", rel="pdf_exports/龙潭站水位数据_97683e68.pdf", size=320993):
    """pdf_export 成功结果（含 generated_files —— 与 provide_file 同形状交付载荷）。

    卡片只带工作区相对 rel_path（2026-09-19 契约：绝不带系统绝对路径）。
    """
    return {
        "name": "pdf_export",
        "result": json.dumps(
            {
                "success": True,
                "action": "export_note",
                "filename": filename,
                "size": size,
                "generated_files": [
                    {"name": filename, "rel_path": rel, "size": size, "type": "pdf"}
                ],
            },
            ensure_ascii=False,
        ),
    }


def _tr_provide_same_pdf(filename="龙潭站水位数据_97683e68.pdf", rel="pdf_exports/龙潭站水位数据_97683e68.pdf", size=320993):
    return _tr("provide_file", [
        {"name": filename, "rel_path": rel, "size": size, "type": "pdf"},
    ])


def test_pdf_export_delivery_surfaces_download_card():
    """验收 #1：pdf_export 成功导出（conv b8dcedc8 场景）→ 出卡。"""
    out = _collect_download_attachments([_tr_pdf_export()])
    assert [a["name"] for a in out] == ["龙潭站水位数据_97683e68.pdf"], out
    assert out[0]["size"] == 320993
    assert out[0]["type"] == "pdf"


def test_pdf_export_card_is_workspace_relative_not_absolute():
    """Security（评审 I）：卡片不得携带系统绝对路径，只带 rel_path。"""
    out = _collect_download_attachments([_tr_pdf_export()])
    assert out[0].get("rel_path") == "pdf_exports/龙潭站水位数据_97683e68.pdf"
    assert "path" not in out[0] or not str(out[0]["path"]).startswith("/"), out[0]


def test_pdf_export_plus_provide_file_same_name_single_card():
    """验收 #1：导出后补调 provide_file（提示词兜底）同名去重=1 张卡。"""
    trs = [_tr_pdf_export(), _tr_provide_same_pdf()]
    out = _collect_download_attachments(trs)
    assert [a["name"] for a in out] == ["龙潭站水位数据_97683e68.pdf"], out


def test_worker_export_plus_provide_dedups_to_one():
    """评审 I-1（worker 路径）：export→provide_file 配对在后台任务也只 1 条。"""
    trs = [_tr_pdf_export(), _tr_provide_same_pdf()]
    out = AgentWorker._extract_attachments(object(), trs)
    assert [a["name"] for a in out] == ["龙潭站水位数据_97683e68.pdf"], out


def test_analysis_byproducts_still_excluded():
    """2026-09-18 事故面不变：terminal/execute_code/code_execution 产物仍不出卡。"""
    trs = [
        _tr("terminal", [{"name": "README.md", "path": "/ws/_refs/opencode/README.md", "size": 111}]),
        _tr("execute_code", [{"name": "chart.png", "path": "/ws/chart.png", "size": 333}]),
        _tr("code_execution", [{"name": "stats.txt", "path": "/ws/stats.txt", "size": 444}]),
    ]
    assert _collect_download_attachments(trs) == []


def test_pdf_export_malformed_result_no_crash():
    trs = [
        {"name": "pdf_export", "result": "not json"},
        {"name": "pdf_export", "result": json.dumps({"success": False, "error": "x"})},
    ]
    assert _collect_download_attachments(trs) == []


def test_pdf_export_failed_result_with_files_yields_no_card():
    """评审 M：失败结果即便携带 generated_files 也不出卡（success 闸）。"""
    trs = [{
        "name": "pdf_export",
        "result": json.dumps({
            "success": False,
            "error": "boom",
            "generated_files": [{"name": "ghost.pdf", "rel_path": "pdf_exports/ghost.pdf", "size": 1}],
        }),
    }]
    assert _collect_download_attachments(trs) == []
    assert AgentWorker._extract_attachments(object(), trs) == []


def test_worker_pdf_export_delivery_surfaces_card():
    """后台任务同策略（agent_worker._extract_attachments）。"""
    out = AgentWorker._extract_attachments(object(), [_tr_pdf_export()])
    assert [a["name"] for a in out] == ["龙潭站水位数据_97683e68.pdf"], out
