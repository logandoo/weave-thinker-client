"""Tests — repair_dangling_citation_rows_20260910（全库悬挂行 wave-2 修复）.

裁决依据（2026-09-10 生产库逐行阅读实证，见脚本 docstring）：
  修：e6b9e64c {11}（[12] 有其文内参考文献条目，保留）、d24f99c7 {13}、
      1803b4ee {11..15}、9b175329 {26}、49a7dfc4 {19}
  不修：f1407235（[0]=未围栏代码 getsockname()[0] 下标）、
        5d9f2747（用户自建的引用错位分析表数据）

Required behavior:
  T1 plan_row 剥离指定 id 集（围栏掩码），其余 [N] 与文本不动；
     display_sequence/content_segments 同步。
  T2 幂等：已修复行再 plan → 零变化。
  T3 REPAIR_MAP 不包含两个 do-not-touch 行。
  T4 e6b9e64c 形态：[11] 剥除且 [12] 保留（文内参考文献条目存在）。

Run: cd backend && python -m pytest tests/test_repair_dangling_rows_20260910.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.repair_dangling_citation_rows_20260910 import REPAIR_MAP, plan_row  # noqa: E402


def _tr(content):
    return json.dumps({
        "results": [{"id": i, "title": f"t{i}", "url": f"http://e{i}.x/", "snippet": "s"} for i in range(1, 11)],
        "display_sequence": [{"type": "text", "content": content}],
        "content_segments": [content],
    }, ensure_ascii=False)


def test_t1_strips_only_the_ruled_ids():
    content = "技术参数如下 [10][11]：与市场 41.6 亿美元 [12]。代码 `a[11]` 不动。"
    new_content, new_tr, plan = plan_row(content, _tr(content), {11})
    assert "[11]" not in new_content.replace("`a[11]`", "")
    assert "`a[11]`" in new_content  # 围栏保护
    assert "[10]" in new_content and "[12]" in new_content  # 其余不动
    assert plan["changed"] and plan["surfaces_fixed"] == 3
    tr = json.loads(new_tr)
    assert "[11]" not in tr["display_sequence"][0]["content"].replace("`a[11]`", "")
    assert "[11]" not in tr["content_segments"][0].replace("`a[11]`", "")


def test_t2_idempotent():
    content = "再组合 [19]。其余 [3] 不动。"
    c1, t1, _ = plan_row(content, _tr(content), {19})
    c2, t2, plan2 = plan_row(c1, t1, {19})
    assert c2 == c1 and json.loads(t2) == json.loads(t1)
    assert not plan2["changed"]


def test_t3_do_not_touch_rows_absent():
    assert "f1407235-be3f-4c78-9b42-475b970a2d94" not in REPAIR_MAP
    assert "5d9f2747-b6f0-4bdc-80e0-4f6bb3d1ca0a" not in REPAIR_MAP


def test_t4_e6b9e64c_keeps_bibliography_backed_12():
    content = "驱动力之一 [12]。参数如下 [10][11]："
    new_content, _, _ = plan_row(content, _tr(content), REPAIR_MAP["e6b9e64c-ba62-4a6b-85fc-8c6073747e6a"])
    assert "[12]" in new_content  # 文内参考文献有条目 → 保留
    assert "[11]" not in new_content
    assert "[10]" in new_content
