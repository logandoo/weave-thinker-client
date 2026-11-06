"""语音回合进度持久化——单调累计契约（conv 4e159a79 同族排查，2026-10-01）。

`_persist_turn_progress` 每次**整体替换** msg.content。审计结论（✅ 无收缩）：
mid-loop 四处调用传 `full_reply + reply_text`（累计；P1b 新增了 act-gate 拒绝
分支），循环两出口先 `full_reply += reply_text` 折叠再 final 传 `full_reply`
—— content 恒单调增长。
本测试钉死接线契约：所有调用点首参必须以 `full_reply` 为根的累计表达式，
防止未来编辑漏掉 `reply_text` 造成部分快照覆盖（conv 4e159a79 数据丢失类）。

Run: cd backend && python -m pytest tests/test_voice_turn_progress_cumulative.py -v
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SERVICE_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "services")
_SRC = "\n".join(
    open(os.path.join(_SERVICE_DIR, name), encoding="utf-8").read()
    for name in ("voice_service.py", "voice_generation.py")
)

_CALL_RE = re.compile(r"_persist_turn_progress\(\s*\n?\s*([^,\n]+)")


def test_all_call_sites_pass_cumulative_content():
    """每个调用点的 content 首参都必须是 full_reply（累计根）表达式。"""
    sites = []
    for m in _CALL_RE.finditer(_SRC):
        line = _SRC[: m.start()].splitlines()[-1]
        if "def " in line:
            continue  # 方法定义行
        sites.append(m.group(1).strip())
    assert len(sites) == 5, f"调用点数量变化（预期 5，实际 {len(sites)}）——请同步更新本契约"
    for i, first_arg in enumerate(sites):
        arg = first_arg.strip()
        assert arg.startswith("full_reply"), (
            f"调用点 #{i} 首参为 {arg!r}——必须传累计文本（full_reply[+reply_text]），"
            "部分快照会整体替换 msg.content 造成用户可见历史收缩"
        )


def test_finalize_folds_reply_text_before_persist():
    """循环两出口都必须先 `full_reply += reply_text` 再退出，保证 final 持久化
    不丢最后一段。"""
    folds = re.findall(r"^\s+full_reply \+= reply_text\s*$", _SRC, re.MULTILINE)
    assert len(folds) == 2, f"折叠点预期 2 处（continue 前 + break 前），实际 {len(folds)}"
    # final 持久化必须发生在折叠之后（源码顺序）
    last_fold = _SRC.rfind("full_reply += reply_text")
    final_persist = _SRC.find("_persist_turn_progress(full_reply, _tc_json, _tr_json, final=True)")
    assert 0 < last_fold < final_persist, "final 持久化先于最后折叠——会丢最后一段"
