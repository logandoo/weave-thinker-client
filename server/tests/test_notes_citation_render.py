"""notes 工具内建引用渲染（已知限制 #1 修复，TDD RED）。

create_note/update_note 全量写入时：内容含 [N] 且本轮有检索条目 → 先渲染为
最终形态（正文变换+参考文献节）再落库；无角标/无条目 → 原样。
append_note 分块写不渲染（参考文献节须在文末，分块无法定位终稿——agent 应
对长文档显式调用 citation_render）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.tools.notes import _maybe_render_note_citations

TR = json.dumps({"results": [
    {"url": "https://example.com/report", "title": "中国人工智能产业发展报告", "published_date": "2025-03-01"},
    {"url": "https://arxiv.org/abs/1706.03762", "title": "Attention Is All You Need", "published_date": "2017",
     "author": "Vaswani, Ashish; Shazeer, Noam; Parmar, Niki"},
]})


def test_renders_when_markers_and_entries():
    out = _maybe_render_note_citations("报告 [1]，研究 [2]。", {"current_turn_tool_results": TR})
    assert "<sup>[1]</sup>" in out
    assert "参考文献" in out
    assert "[EB/OL]" in out


def test_passthrough_without_markers():
    out = _maybe_render_note_citations("普通笔记内容。", {"current_turn_tool_results": TR})
    assert out == "普通笔记内容。"


def test_passthrough_without_entries():
    out = _maybe_render_note_citations("引用 [1] 但无检索。", {"current_turn_tool_results": ""})
    assert "参考文献" not in out


def test_strips_model_bibliography_before_render():
    content = "结论 [1]。\n\n## 参考来源\n\n[1] 模型自写旧条目. 2024."
    out = _maybe_render_note_citations(content, {"current_turn_tool_results": TR})
    assert "模型自写旧条目" not in out
    assert out.count("## 参考文献") == 1


def test_zero_citation_content_no_section():
    out = _maybe_render_note_citations("没有引用标记的正文。", {"current_turn_tool_results": TR})
    assert "参考文献" not in out


def test_kwargs_missing_key_safe():
    out = _maybe_render_note_citations("引用 [1]。", {})
    assert "[1]" in out and "参考文献" not in out
