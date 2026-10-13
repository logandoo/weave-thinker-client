"""正文豁免瘦身（实施波 A1，2026-10-03）

红线：回答正文（display_sequence 中 type=='text' 的 content）永不被 slim 预览切片。
工具/思考 content 允许保留 INNER_PREVIEW_CHARS 预览（折叠卡后有展开取全文链路）。

运行: cd backend && ../.venv/bin/python -m pytest tests/test_body_exempt_slim.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.message_payload_service import (  # noqa: E402
    INNER_PREVIEW_CHARS,
    build_field_stub,
    slim_tool_results_json,
)

BODY_LEN = 5841  # conv 8c03ff8e/0836a828 实际正文字数
BODY = "正文" + "字" * (BODY_LEN - 2)


def _seq(body=BODY, tool_len=3969, reasoning_len=65704):
    return {
        "display_sequence": [
            {"type": "reasoning_step", "title": "思考", "content": "思" * reasoning_len},
            {"type": "tool", "name": "session_search", "title": "工具", "content": "{" + "x" * tool_len + "}"},
            {"type": "text", "content": body},
        ],
        "content_segments": [body],
        "results": [{"url": "https://example.com", "snippet": "y" * 10}],
    }


def test_text_body_survives_slim():
    """正文段必须原样通过瘦身器——不许 [:500]。"""
    slim = slim_tool_results_json(_seq())
    text_item = slim["display_sequence"][2]
    assert text_item["type"] == "text"
    assert len(text_item["content"]) == BODY_LEN, (
        f"正文被切到 {len(text_item['content'])} 字（预期 {BODY_LEN}）——静默截断红线"
    )
    assert text_item["content"] == BODY
    assert not text_item.get("__truncated__")


def test_tool_content_still_previewed():
    """工具结果预览行为保持（性能收益不回吐）。"""
    slim = slim_tool_results_json(_seq())
    tool_item = slim["display_sequence"][1]
    assert len(tool_item["content"]) == INNER_PREVIEW_CHARS
    assert tool_item.get("__truncated__") is True
    # N=Σ内容键字符串（双侧同构口径定稿，A4.9 C2 fix，test-change）：
    # title(工具=2) + content 全量({"+x*3969+} = 3971)；标签键（type/name/status）不计
    assert tool_item.get("__size_bytes__") == len("工具") + (1 + 3969 + 1)


def test_reasoning_content_still_previewed():
    slim = slim_tool_results_json(_seq())
    r = slim["display_sequence"][0]
    assert len(r["content"]) == INNER_PREVIEW_CHARS
    assert r.get("__truncated__") is True


def test_content_segments_list_stays_full():
    slim = slim_tool_results_json(_seq())
    assert len(slim["content_segments"][0]) == BODY_LEN


def test_short_body_untouched():
    body = "短回答。"
    slim = slim_tool_results_json(_seq(body=body))
    assert slim["display_sequence"][2]["content"] == body


def test_build_field_stub_carries_full_body():
    """写入期桩（build_field_stub）同样不许切正文。"""
    stub = json.loads(build_field_stub("tool_results", json.dumps(_seq(), ensure_ascii=False)))
    assert stub.get("_externalized") is True
    text_item = stub["display_sequence"][2]
    assert len(text_item["content"]) == BODY_LEN
    assert stub["content_segments"][0] == BODY


def test_body_exemption_scoped_to_display_sequence():
    """树作用域（评审 R2 I2）：display_sequence 之外的 type:"text" 字典不得豁免。"""
    rogue = {"results": [{"type": "text", "content": "外" * 3000}]}
    slim = slim_tool_results_json(rogue)
    item = slim["results"][0]
    assert len(item["content"]) == INNER_PREVIEW_CHARS
    assert item.get("__truncated__") is True
    # 对照：同形字典在 display_sequence 内豁免
    inner = slim_tool_results_json({"display_sequence": [{"type": "text", "content": "内" * 3000}]})
    assert len(inner["display_sequence"][0]["content"]) == 3000


def test_n3_exemption_not_sticky_into_nested_dicts():
    """复审 N3：豁免只及 display_sequence 直系项——项内嵌套 type:"text" 字典照切。"""
    obj = {"display_sequence": [{"type": "tool", "content": {"inner": {"type": "text", "content": "嵌" * 3000}}}]}
    slim = slim_tool_results_json(obj)
    inner = slim["display_sequence"][0]["content"]["inner"]
    assert len(inner["content"]) == INNER_PREVIEW_CHARS
    assert inner.get("__truncated__") is True
