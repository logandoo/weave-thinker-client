"""存量修复器行为测试（A4.9 波 C 复审 I5/I6/I7 防护钉死）。

Run: cd backend && python -m pytest tests/test_repair_duplicated_final_answers.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.repair_duplicated_final_answers import detect_and_fix


def test_narr_strips_process_opener():
    fixed, kind, _ = detect_and_fix(
        "我先看看这两个引擎的官方仓库文档，确认上下文上限的具体数值。"
        + "两个引擎都是为 Strix Halo 专门优化的本地推理引擎，跑的都是 Qwen3.8-Flash-Next。" * 2
    )
    assert kind == "NARR"
    assert fixed.startswith("两个引擎都是")


def test_narr_keeps_perfective_findings():
    """I7：完成体「了」句是信息性首句——不得剥。"""
    fixed, kind, _ = detect_and_fix(
        "我先查了一下你的配置，发现三处问题。第一处是数据库连接串写错了，第二处是缓存未启用，第三处是日志级别过高。"
    )
    assert kind == "none"
    assert fixed.startswith("我先查了一下")


def test_narr_strips_segment_marker():
    fixed, kind, _ = detect_and_fix(
        "我先查看一下笔记本列表。\n\n<!-- segment_split -->\n\n"
        + "已找到「我的主笔记本」，ID 为 ac7c9236，可在后续操作中直接引用。" * 3
    )
    assert kind == "NARR"
    assert fixed.startswith("已找到")


def test_dup_cuts_true_restart_pair():
    body = "## 分析\n\n**结论**：答案内容" + "详细说明。" * 60
    dup = body + "\n\n" + body
    fixed, kind, note = detect_and_fix(dup)
    assert kind == "DUP"
    assert fixed == body


def test_dup_coverage_gate_blocks_quote_led():
    """I5：引用块开头+中后重现的合法单稿不切（覆盖率门）。"""
    quote = "「引用文字" + "样。" * 60 + "」"
    rest = "正文分析。" * 50 + "\n\n**结论**：完成。"
    single = quote + rest + "\n\n补充再次引用开头：" + quote[:150]
    fixed, kind, _ = detect_and_fix(single)
    assert kind in ("none", "REVIEW"), f"合法单稿被误判 {kind}"
    assert len(fixed) == len(single)


def test_cut_at_guard_is_in_source():
    """I6：手术口越界防护 + 与 --message-id 绑定 + 备份。"""
    import scripts.repair_duplicated_final_answers as rp

    src = open(rp.__file__, encoding="utf-8").read()
    assert "0 < args.cut_at < len(content) - 60" in src
    assert "repair_backup_" in src
