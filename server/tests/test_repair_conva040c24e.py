"""RED tests (A4.8) — conv a040c24e 存量引用错位修复脚本（msg baab73dc）.

生产证据（60 服务器 DB 实证，2026-09-10 取证）：msg
baab73dc-ff9d-4909-acec-1c788d0a36e2 正文含 "[1][11]"×2 + "[3][11]" +
"[3][11][7]"；results_n=10 → [11] 越界无卡片；[1] 位置映射为
电摩之家 M3 100（与"素车/选装"论断无关）。引用意图来自上上轮
fd417997 台账（[1]=smzdm 素车文、[11]=zeeho 配件价格查询），本轮
results[] 无对应条目 → 确定性可修 = 剥除这 4 处标记（conv 3b58af5b
wave-2「悬挂 [N] 全剥、cited 全可解析」先例）。

Required behavior:
  T1 plan_row 纯函数："[1][11]"→剥除（2 处），残余 "[11]"→剥除（[3][11]→[3]、
     [3][11][7]→[3][7]）；其余 [N] 不动；content_segments/display_sequence
     文本表面同步修复。
  T2 修复后该行 cited [N] 全部 ≤ results_n（零悬挂），[1] 不再出现。
  T3 幂等：plan_row 应用于已修复行 → 零变化。
  T4 scan_row：修复前报告 {11}（及语义误指的提示），修复后零悬挂；
     枚举括号（3）不报。

Run: cd backend && python -m pytest tests/test_repair_conva040c24e.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.repair_conv_a040c24e_citations import plan_row, scan_row  # noqa: E402

CONTENT = (
    "三车价格仅相差 100—600 元，处于同一预算带 [3][11]。\n"
    "M5 100 与 AE6 Pro 电池完全相同（72V32Ah）[3][11][7]。\n"
    "0-50km/h 约 5.2 秒 [3][7][9]。\n"
    "关键差异：AE6 Pro 是“素车”，哨兵、双录、胎压全为选装或没有 [1][11]；"
    "九号 M5 100 智能生态是强项 [2][8]。\n"
    "需留意 Pro 是素车、这些要额外选装或另配 [1][11]。"
)

RESULTS = [{"id": i, "title": f"t{i}", "url": f"http://e{i}.example/", "snippet": "s"} for i in range(1, 11)]


def _tr_json():
    return json.dumps({
        "results": RESULTS,
        "agent_steps": [{"name": "c1", "title": "联网搜索 (第1轮)", "content": "1. t1\nURL: http://e1.example/\n摘要: s", "step_type": "tool"}],
        "display_sequence": [{"type": "text", "content": CONTENT}],
        "content_segments": [CONTENT],
    }, ensure_ascii=False)


def test_t1_plan_row_strips_documented_markers():
    new_content, new_tr_json, plan = plan_row(CONTENT, _tr_json())
    assert "[1][11]" not in new_content
    assert "[11]" not in new_content
    assert "[3][7]" in new_content  # [3][11][7] → [3][7]
    assert "预算带 [3]。" in new_content  # [3][11] → [3]
    assert "[3][7][9]" in new_content and "[2][8]" in new_content  # 其余不动
    assert plan["removed_joint"] == 2 and plan["removed_dangling"] == 2
    tr = json.loads(new_tr_json)
    ds_text = tr["display_sequence"][0]["content"]
    seg = tr["content_segments"][0]
    assert "[11]" not in ds_text and "[11]" not in seg
    assert "[1][11]" not in ds_text and "[1][11]" not in seg


def test_t2_no_dangling_after_repair():
    new_content, new_tr_json, _ = plan_row(CONTENT, _tr_json())
    dangling = scan_row(new_content, new_tr_json)
    assert dangling == set(), f"修复后仍悬挂: {dangling}"
    assert "[1]" not in new_content


def test_t3_idempotent_on_repaired_row():
    c1, t1, _ = plan_row(CONTENT, _tr_json())
    c2, t2, plan2 = plan_row(c1, t1)
    assert c2 == c1 and json.loads(t2) == json.loads(t1)
    assert plan2["removed_joint"] == 0 and plan2["removed_dangling"] == 0


def test_t4_scan_flags_original_not_repaired():
    assert scan_row(CONTENT, _tr_json()) == {11}
    new_content, new_tr_json, _ = plan_row(CONTENT, _tr_json())
    assert scan_row(new_content, new_tr_json) == set()


def test_t4_scan_ignores_fullwidth_enumeration():
    content = "要点 （3） 与 （12） 是枚举，不是引用。"
    tr = json.dumps({"results": RESULTS})
    assert scan_row(content, tr) == set()


def test_t5_nondefault_msg_requires_force(monkeypatch, capsys):
    """A4.9 R1 M6：--msg 指向非文档行时必须 --force 二次确认（防误剥合法引用）。"""
    from scripts.repair_conv_a040c24e_citations import main

    monkeypatch.setattr("sys.argv", ["repair", "--msg", "c8562c6e-fcc3-4944-b23e-079fa98375f0"])
    rc = main()
    assert rc == 2
    assert "refused" in capsys.readouterr().out
