"""重放探针（acceptance 判据 5）：conv a104fbc5 长消息意图窗口 head-only vs 逐字全文。

用真实生产输入（DB 消息 218 尾部诉求 + 消息 219 被审草稿，去警示语）重放：
  A. 协调器：旧「前 800 字符」 vs 新 `_coordinator_recent_messages`（逐字全文）→ focus 判读
  B. 审计员：旧 head-only + 旧 focus vs 新全文 + 新 focus → verdict/problem
真实 LLM（生产路由 purpose=coordinator / agent.audit；temperature 与生产一致）。

Run: cd backend && ../.venv/bin/python tests/probe_intent_window_20260921.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.gateway import get_model_gateway  # noqa: E402
from app.services.agent_loop import (  # noqa: E402
    AgentLoop,
    _coordinator_recent_messages,
)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_USER_MSG = open(os.path.join(_REPO, "tests", "a104_user_msg218.txt"), encoding="utf-8").read().rstrip("\n")
_DRAFT_RAW = open(os.path.join(_REPO, "tests", "a104_asst_msg219.txt"), encoding="utf-8").read()
# 去掉出货时追加的「未通过自动质量检查」说明，还原第二次审计时的 1858 字符草稿
_DRAFT = _DRAFT_RAW.split("\n\n\n---\n（说明：本回答在一次修正后")[0].rstrip("\n")

assert "受限估计是为什么" in _USER_MSG, "真实用户消息尾部诉求缺失"
assert "自动质量检查" not in _DRAFT, "草稿未去掉出货说明"


def _extract_json(raw: str) -> dict:
    s = raw.find("{")
    e = raw.rfind("}") + 1
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s:e])
    except json.JSONDecodeError:
        return {}


async def coordinator_focus(gw, user_view: str) -> str:
    system_prompt = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE.format(
        identity_clause="没有用户自定义名称时就以'罗航'自称。严禁编造不存在的名称。\n")
    raw = await gw.chat(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": user_view}],
        purpose="coordinator", temperature=0.3,
    )
    return str(_extract_json(raw).get("focus", "") or "")


async def auditor_verdict(gw, context: str) -> dict:
    system = AgentLoop._AUDITOR_SYSTEM_TEMPLATE.replace("__ASSISTANT_NAME__", "AI助手")
    raw = await gw.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": context}],
        purpose="agent.audit", temperature=0.0,
    )
    js = _extract_json(raw)
    return {"verdict": js.get("verdict", ""), "problem": (js.get("problem") or "")[:200]}


async def main() -> int:
    gw = get_model_gateway()
    print("=" * 72)
    print("A. 协调器判读对照（真实消息 6182 字符）")
    old_view = _USER_MSG[:800]
    new_view = _coordinator_recent_messages(
        [{"role": "user", "content": _USER_MSG}])[-1]["content"]
    assert new_view == _USER_MSG, "新路径必须逐字全文（零截断）"
    old_focus = await coordinator_focus(gw, old_view)
    print(f"[OLD head-only 800] focus={old_focus}")
    new_focus = await coordinator_focus(gw, new_view)
    print(f"[NEW 逐字全文     ] focus={new_focus}")

    # 「受限估计」一词在粘贴头部就存在（表体），不能作为尾部诉求的信号
    tail_markers = ("为什么", "批评", "糟糕", "废", "质疑", "追问", "不满")
    old_tail = any(m in old_focus for m in tail_markers)
    new_tail = any(m in new_focus for m in tail_markers)
    print(f"→ 尾部诉求可见: OLD={old_tail} · NEW={new_tail}")

    print("=" * 72)
    print("B. 审计员判决对照（真实草稿 1858 字符）")
    old_ctx = (
        f"用户最新消息：{old_view}\n\n"
        f"本轮意图（协调器判断）：{old_focus}\n\n"
        f"助手草稿回答：{_DRAFT}"
    )
    new_ctx = (
        f"用户最新消息：{new_view}\n\n"
        f"本轮意图（协调器判断）：{new_focus}\n\n"
        f"助手草稿回答：{_DRAFT}"
    )
    old_verdicts = [await auditor_verdict(gw, old_ctx) for _ in range(2)]
    new_verdicts = [await auditor_verdict(gw, new_ctx) for _ in range(2)]
    for i, v in enumerate(old_verdicts, 1):
        print(f"[OLD {i}] verdict={v['verdict']} problem={v['problem']}")
    for i, v in enumerate(new_verdicts, 1):
        print(f"[NEW {i}] verdict={v['verdict']} problem={v['problem']}")

    old_reject = any(v["verdict"] == "reject" for v in old_verdicts)
    new_reject = any(v["verdict"] == "reject" for v in new_verdicts)
    ok = (new_tail and not new_reject)
    print("=" * 72)
    print(f"VERDICT: old_window_reproduces_intent_blindness={not old_tail or old_reject} · "
          f"new_window_intent_visible={new_tail} · new_window_no_hard_reject={not new_reject} · "
          f"PROBE {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
