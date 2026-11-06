"""RED (2026-10-22, 60 生产 conv 5c89e092 症状2)：模型输出 `[cite:10,11,...]`
原生引用标记未被任何管线处理——content 首尾各一行裸标记直达用户。

修复方向（SOTA=CiteFix/grounded-citations 同族：系统持有 url→[N] 台账，后处理
校正模型引用）：规范化 `[cite:10, 11]` → `[10][11]` 后走既有 verify/sanitize
管线——合法编号保留为内联引用（前端徽章可渲染），越界编号按 unknown 清理；
围栏/行内代码内不动；非数字内容不动。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.citation_ledger import CitationLedger, normalize_cite_markers


def _ledger(n=20):
    led = CitationLedger()
    for i in range(1, n + 1):
        led.register(f"https://example.com/src/{i}", title=f"来源{i}")
    return led


def test_normalize_single_and_multi_ids():
    text = "[cite:10]开头\n[cite:10,11,12,13,14,15,16]\n结尾[cite: 3, 5]"
    out = normalize_cite_markers(text)
    assert out == "[10]开头\n[10][11][12][13][14][15][16]\n结尾[3][5]"


def test_normalize_case_insensitive_and_spaces():
    assert normalize_cite_markers("[CITE:1,2]") == "[1][2]"
    assert normalize_cite_markers("[cite: 7 , 8 ]") == "[7][8]"


def test_normalize_leaves_non_numeric_and_fences_untouched():
    text = "[cite:abc] [cite:] [cite:1,x]\n```\n[cite:1,2]\n```\n`[cite:9]`"
    out = normalize_cite_markers(text)
    # 非数字内容不是合法引用标记，保守保留原文
    assert "[cite:abc]" in out and "[cite:]" in out and "[cite:1,x]" in out
    # 围栏/行内代码内一律不动
    assert "```\n[cite:1,2]\n```" in out
    assert "`[cite:9]`" in out



def test_sanitize_keeps_valid_cite_ids_as_inline_refs():
    led = _ledger(20)
    text = "结论见下方引用。\n\n[cite:10,11,12,13,14,15,16]\n\n正文 [3]。"
    out = led.sanitize(text)
    assert "[cite:" not in out
    assert "[10][11][12][13][14][15][16]" in out
    assert "[3]" in out


def test_sanitize_strips_out_of_range_cite_ids():
    led = _ledger(20)
    text = "正文。\n[cite:10,97,98]"
    out = led.sanitize(text)
    assert "[cite:" not in out
    assert "[10]" in out
    assert "[97]" not in out and "[98]" not in out


def test_verify_counts_cite_marker_ids():
    """审计映射视图一致性：verify 须把 [cite:...] 内合法编号计入 cited。"""
    led = _ledger(20)
    report = led.verify("草稿引用了 [cite:10,11] 与 [97]。")
    assert {10, 11} <= report.cited
    assert 97 in report.unknown


def test_sanitize_texts_all_surfaces_consistent():
    led = _ledger(20)
    texts = ["content 面 [cite:1,2]", "segment 面 [cite:3]", "display 面 [cite:4,5]"]
    out = asyncio.run(led.sanitize_texts(texts))
    assert out == ["content 面 [1][2]", "segment 面 [3]", "display 面 [4][5]"]


def test_sanitize_cited_content_end_to_end_no_cite_residue():
    """持久化端到端：content + display_sequence 文本面均无 [cite: 残留。"""
    from app.api.chat_citations import _sanitize_cited_content

    tr_obj = {
        "results": [
            {"url": f"https://example.com/src/{i}", "title": f"来源{i}",
             "snippet": "", "author": "", "year": ""}
            for i in range(1, 21)
        ],
        "content_segments": ["段内引用 [cite:2,3]"],
        "display_sequence": [
            {"type": "text", "content": "[cite:10,11,12,13,14,15,16]\n\n答案正文 [10]。"},
            {"type": "tool", "content": "工具卡不含标记"},
        ],
    }
    content = "[cite:10,11,12,13,14,15,16]\n\n答案正文 [10]。\n\n[cite:10,11,12,13,14,15,16]"
    new_content, new_tr = asyncio.run(
        _sanitize_cited_content(content, json.dumps(tr_obj, ensure_ascii=False))
    )
    assert "[cite:" not in new_content
    assert new_content.count("[10][11][12][13][14][15][16]") == 2
    obj = json.loads(new_tr)
    assert "[cite:" not in json.dumps(obj, ensure_ascii=False)
    ds0 = obj["display_sequence"][0]["content"]
    assert ds0.startswith("[10][11][12][13][14][15][16]")


def test_strip_stop_citation_echo_normalizes_cite_markers():
    """stop 路径（无 LLM judge）同样规范化 [cite: 标记。"""
    from app.api.chat_citations import strip_stop_citation_echo

    raw_results = [
        {"name": "web_search",
         "result": json.dumps({"results": [
             {"url": f"https://example.com/src/{i}", "title": f"来源{i}"}
             for i in range(1, 21)]}, ensure_ascii=False)},
    ]
    content = "答案 [cite:5,6]。"
    out = strip_stop_citation_echo(content, raw_results)
    assert "[cite:" not in out
    assert "[5][6]" in out


def test_normalize_consumes_link_and_reference_suffixes():
    """遗留项收口（re-review Minor-1 族）：`[cite:N](url)` / `[cite:N]: ref`
    不再保持原样（那会让伪造编号对 verify/backfill 永久隐身）——消费链接/引用
    后缀，只留台账形态 `[N]`（合法→徽章，越界→unknown 清理）。"""
    assert normalize_cite_markers("[cite:3](http://x.com)") == "[3]"
    assert normalize_cite_markers("[cite:3,4](http://x.com) 尾部") == "[3][4] 尾部"
    assert normalize_cite_markers("[cite:97](http://evil)") == "[97]"
    # 行首 reference-definition → 整行消费
    assert normalize_cite_markers("[cite:5]: https://ref.example/a") == "[5]"
    # re-review I-3：行内「引用+冒号+解释」不吞正文，也不吞同行第二个标记
    assert normalize_cite_markers("见 [cite:3]: 这句话是正文") == "见 [3]: 这句话是正文"
    assert normalize_cite_markers("同行 [cite:3]: 说明 [cite:4]") == "同行 [3]: 说明 [4]"
    # 一层嵌套括号的链接目标完整消费（不残留孤括号）
    assert normalize_cite_markers(
        "[cite:3](https://en.wikipedia.org/wiki/Foo_(bar))") == "[3]"
    # 围栏内仍不动（示例代码保形）
    assert "```\n[cite:3](http://x)\n```" in normalize_cite_markers(
        "```\n[cite:3](http://x)\n```")
    # 全角冒号的普通正文不受影响
    assert normalize_cite_markers("[cite:6]：说明") == "[6]：说明"


def test_strip_cite_markers_removes_all_marker_forms():
    """零工具活动回合（无台账 → 任何 [cite:] 都是伪造引用）：整枚剥除。"""
    from app.services.citation_ledger import strip_cite_markers

    assert strip_cite_markers("答案。\n[cite:10,11]\n尾注") == "答案。\n\n尾注"
    # 行内冒号是正文（I-3）：只剥标记，保留「: ref」文字
    assert strip_cite_markers("见[cite:3](http://x)与[cite:4]: ref") == "见与: ref"
    # 行首 reference-definition 整行剥除
    assert strip_cite_markers("[cite:4]: https://x\n正文") == "\n正文"
    assert strip_cite_markers("`[cite:9]` 与 ```\n[cite:8]\n```") == "`[cite:9]` 与 ```\n[cite:8]\n```"
    assert strip_cite_markers("无标记") == "无标记"


def test_sanitize_cited_content_zero_activity_strips_cite_markers():
    """零工具活动（无 results 且无 agent_steps）→ 直接剥 [cite:]，不依赖台账。"""
    from app.api.chat_citations import _sanitize_cited_content

    content = "[cite:10,11]\n\n没有检索的一轮答案。\n[cite:10]"
    tr = json.dumps({
        "results": [],
        "content_segments": ["段内 [cite:3]"],
        "display_sequence": [{"type": "text", "content": "[cite:7] 过程面"}],
    }, ensure_ascii=False)
    new_content, new_tr = asyncio.run(_sanitize_cited_content(content, tr))
    assert "[cite:" not in new_content
    assert "[cite:" not in new_tr
    obj = json.loads(new_tr)
    assert obj["content_segments"] == ["段内 "]
    assert obj["display_sequence"][0]["content"] == " 过程面"


def test_inline_ref_colon_fabricated_id_is_cleaned():
    """re-review N-2：行内 `[cite:97]:` 规范化后成 `[97]:`，`_CITE_RE` 按设计
    跳过 — 旧实现在 verify 里永远看不到该编号 → 伪造引用存活。现在 verify
    直接采集 `[cite:…]` 原始编号，sanitize 先剔除伪造编号再规范化。"""
    led = _ledger(3)

    assert led.verify("[cite:97]: x").unknown == {97}
    assert led.sanitize("见 [cite:97]: 说明") == "见 : 说明"
    assert "[97]" not in led.sanitize("见 [cite:97]: 说明")
    # 行首 ref-definition 整行剔除
    assert led.sanitize("行首\n[cite:97]: https://evil") == "行首\n"
    # 合法编号保留（行内冒号是正文）
    assert led.sanitize("[cite:3]: 正文") == "[3]: 正文"
    # 混合列表：合法保留、伪造剔除
    assert led.sanitize("[cite:3,97](http://evil)") == "[3]"


def test_ref_def_after_inline_code_does_not_swallow_tail():
    """re-review N-3：片段切分会把行内代码后的位置误当行首；位置掩码修复后
    行尾正文不得被吞。"""
    assert normalize_cite_markers("`code`[cite:9]: rest of line") == "`code`[9]: rest of line"
