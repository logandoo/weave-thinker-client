"""RED tests (A4.8) — 跨轮引用编号污染：历史回放中性化（conv a040c24e, 2026-09-10）.

生产证据（服务器 <cloud-host>，conv a040c24e-8101-4c27-9fbc-66c0f8bbd79a，
用户 魏洋，2026-09-06 00:20 local）：末轮 msg baab73dc 正文
"AE6 Pro 是素车…胎压全为选装或没有 [1][11]" 复用了上上轮 fd417997 的
台账编号（该轮 [1]=smzdm"7999的Pro是素车"文、[11]=zeeho 配件价格查询），
而末轮台账 [1]=电摩之家 M3 100（语义错位）、results_n=10 使 [11] 越界
无卡片。源头：历史轮正文 [N] 与历史轮编号化 formatted 搜索列表经
rebuild_structured_history / _sanitize_history_content 原样回放，新轮
CitationLedger 从 1 重启编号——模型看到的旧编号与新编号形式不可区分。

形态迭代（2026-09-10 部署后生产冒烟 conv b650d7c2）：初版将 [N] 改写为
全角 （N），模型在新答案中**模仿**该形态（"吹不到手 （6）（10）" 无徽章
落库）——任何保数字替换皆可被模仿，故改为**移除**（唯一不可模仿形态）。

Required behavior:
  T1 `neutralize_historical_citations`：历史助手正文 [N] **移除**（枚举数字
     一并移除、命题文本保留；代码围栏 ``` 与行内代码 ` 内的 [N] 不动）。
  T2 `_denumber_search_formatted`：历史 web_search formatted 列表条目头
     `N. `（块内含 `URL: ` 行为结构签名）→ `- `；标题/URL/摘要保留；
     摘要内部以数字开头的行（无签名）不动；多行标题覆盖。
  T3 `rebuild_structured_history` 端到端：assistant 正文 [N] 移除 +
     web_search tool 消息去编号；非 assistant 单消息原样返回（user 不动）。
  T4 共享 helper `build_history_message` 行为契约：assistant 行移除 /
     user 行零改动 / 空 reasoning 不注键；三回放路径接线。

Run: cd backend && python -m pytest tests/test_history_citation_neutralization.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.tool_history import (  # noqa: E402
    _denumber_search_formatted,
    neutralize_historical_citations,
    rebuild_structured_history,
)


# ── T1: prose neutralization ──
def test_t1_prose_markers_removed():
    src = "三车同预算带 [3][11]。素车全为选装或没有 [1][11]；其余如前 [2]。"
    out = neutralize_historical_citations(src)
    assert "[3]" not in out and "[11]" not in out and "[1]" not in out and "[2]" not in out
    # 移除形态（不可替代）：全角形态也不存在——prod 冒烟实证模型会模仿
    assert "（3）" not in out and "（11）" not in out
    assert "素车全为选装或没有 ；" in out


def test_t1_enumeration_loses_only_the_number():
    src = "下面讲 [3] 个要点，第 [12] 条最关键。"
    out = neutralize_historical_citations(src)
    assert "[3]" not in out and "[12]" not in out and "（3）" not in out
    assert "个要点" in out and "条最关键" in out


def test_t1_code_fences_untouched():
    src = "引用 [2] 见上。\n```python\nx = a[1]\ny = b[12]\n```\n行内 `c[3]` 不动，正文 [4] 要改。"
    out = neutralize_historical_citations(src)
    assert "a[1]" in out and "b[12]" in out and "`c[3]`" in out
    assert "[2]" not in out.replace("a[1]", "").replace("b[12]", "").replace("c[3]", "")
    assert "正文  要改" in out


def test_t1_empty_and_none():
    assert neutralize_historical_citations("") == ""
    assert neutralize_historical_citations(None) == ""


# ── T2: formatted search-list de-numbering ──
def test_t2_formatted_list_denumbered():
    formatted = (
        "1. 极核AE6三个版本怎么选：7999的Pro是素车\n"
        "URL: https://post.smzdm.com/p/ad727g5k/\n"
        "摘要: 三个版本对比\n\n"
        "2. 配件价格查询 - zeeho\n"
        "URL: http://www.zeehoev.com/cn/service/spare-parts.html\n"
        "摘要: 选装配件价格"
    )
    out = _denumber_search_formatted(formatted)
    assert "- 极核AE6三个版本怎么选" in out
    assert "- 配件价格查询 - zeeho" in out
    assert "1. 极核" not in out and "2. 配件" not in out
    assert "URL: https://post.smzdm.com/p/ad727g5k/" in out  # 信息保留


def test_t2_non_entry_digit_lines_untouched():
    formatted = (
        "1. 标题A\n"
        "URL: http://a.example/\n"
        "摘要: 2024 年 3 个版本：\n1. 标准版\n2. 高配版"
    )
    out = _denumber_search_formatted(formatted)
    assert "- 标题A" in out
    # 摘要内部以数字开头的行没有 URL: 签名 → 不动
    assert "1. 标准版" in out and "2. 高配版" in out


def test_t2_multiline_title_denumbered():
    """A4.9 R1 M4(a)：标题含换行时，'次行 URL:' 签名失效 → 旧编号残留。
    块级签名（块内含 URL: 行则块首编号行去编号）必须覆盖。"""
    formatted = (
        "1. 标题第一行\n标题续行\n"
        "URL: http://a.example/\n"
        "摘要: s\n\n"
        "2. 正常标题\n"
        "URL: http://b.example/\n"
        "摘要: t"
    )
    out = _denumber_search_formatted(formatted)
    assert "1. 标题第一行" not in out
    assert "- 标题第一行\n标题续行" in out
    assert "- 正常标题" in out


def test_t2_no_results_text_passthrough():
    assert _denumber_search_formatted("未检索到可用网页结果。") == "未检索到可用网页结果。"
    assert _denumber_search_formatted("") == ""


# ── T3: rebuild_structured_history end-to-end ──
def _tool_calls_blob():
    return json.dumps([
        {"id": "call_1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
    ], ensure_ascii=False)


def _tool_results_blob():
    return json.dumps({
        "agent_steps": [
            {
                "name": "call_1",
                "title": "联网搜索 (第1轮)",
                "content": "1. 标题A\nURL: http://a.example/\n摘要: s",
                "step_type": "tool",
            },
        ],
    }, ensure_ascii=False)


def test_t3_rebuild_neutralizes_assistant_and_tool_messages():
    msgs = rebuild_structured_history(
        role="assistant",
        content="答案引用 [1] 与 [11]。",
        tool_calls_json=_tool_calls_blob(),
        tool_results_json=_tool_results_blob(),
    )
    assert msgs[0]["role"] == "assistant"
    assert "答案引用" in msgs[0]["content"]  # 锚点：移除的是标记而非文本
    assert "[1]" not in msgs[0]["content"] and "[11]" not in msgs[0]["content"]
    assert "（1）" not in msgs[0]["content"]  # 移除形态，无全角替代
    tool_msg = msgs[1]
    assert tool_msg["role"] == "tool"
    assert "1. 标题A" not in tool_msg["content"]
    assert "- 标题A" in tool_msg["content"]


def test_t3_user_message_untouched():
    msgs = rebuild_structured_history(role="user", content="请对比 [1] 和 [2] 方案", tool_calls_json=None, tool_results_json=None)
    assert msgs == [{"role": "user", "content": "请对比 [1] 和 [2] 方案"}]


def test_t3_assistant_without_tool_calls_via_chat_path():
    """A4.9 R1 M3：行为级契约——共享 helper `build_history_message` 对
    assistant 行中性化、user 行零改动；chat/worker/scheduler 回放均经由此
    helper（不再是易失效的源码 grep 断言）。"""
    from app.services.tool_history import build_history_message

    a = build_history_message("assistant", "答案引用 [1] 与 [11]。", reasoning_content="r")
    assert a["role"] == "assistant"
    assert "答案引用" in a["content"]
    assert "[1]" not in a["content"] and "[11]" not in a["content"] and "（1）" not in a["content"]
    assert a["reasoning_content"] == "r"

    u = build_history_message("user", "请对比 [1] 和 [2] 方案")
    assert u == {"role": "user", "content": "请对比 [1] 和 [2] 方案"}

    a2 = build_history_message("assistant", "无标记文本")
    assert "reasoning_content" not in a2  # 空 reasoning 不注入键


def test_t4_worker_scheduler_replay_neutralized():
    import inspect

    import app.services.agent_scheduler as sched_mod
    import app.services.agent_worker as worker_mod

    assert "build_history_message" in inspect.getsource(worker_mod), "agent_worker 回放未接入共享 helper"
    assert "build_history_message" in inspect.getsource(sched_mod), "agent_scheduler 回放未接入共享 helper"


def test_t5_voice_and_deathmatch_replay_neutralized():
    """D-12 缺口收口（遗留项①）：voice_service 与 deathmatch_service 的
    历史回放同样接入中性化——voice 路径无台账（[N] 会被 TTS 读出/被模型
    模仿），deathmatch 压缩摘要路径同理。"""
    import inspect
    from pathlib import Path

    import app.services.deathmatch_service as dm_mod
    import app.services.voice_service as voice_mod

    base = Path(voice_mod.__file__).resolve().parent
    vsrc = "".join(p.read_text(encoding="utf-8") for p in base.glob("voice_*.py"))
    dsrc = "".join(p.read_text(encoding="utf-8") for p in base.glob("deathmatch_*.py"))
    assert "neutralize_historical_citations" in vsrc, "voice_service 回放未接入中性化"
    assert "_denumber_search_formatted" in vsrc, "voice_service 工具消息未去编号"
    assert "neutralize_historical_citations" in dsrc, "deathmatch_service 回放未接入中性化"
