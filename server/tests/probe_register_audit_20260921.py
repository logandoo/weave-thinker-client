"""R3 行为探针：审计新增判据（草稿内部语义复述 + 领域语域）的 A/B 对照。

用户两问（conv a104fbc5，2026-09-21）：「回答本身是车轱辘话，而且语句不通顺」
「学术文章的定义应该是假设/验证/结论，怎么现在 agent 写的更像是 coding 的流程？」
A 组（预期不合格）：该会话末答真实全文（tests/register_probe_offending.txt）——
  三节重述同一认错点 + 工程语域（跑/执行/降级或删除/返工/填报/闭环）。
B 组（预期合格，防误伤对照）：同问题的研究语域、互不复述改写
  （tests/register_probe_clean.txt，仍如实引用用户提出的「受限估计」一词）。
C 组（预期不合格，判据 10 单独取证）：研究语域但三节重述同一认错点
  （tests/register_probe_repetitive.txt）。
D 组（预期合格，防误伤-工程类）：非学术的运维/数据分析问答（含"执行/跑"等词）
  （tests/register_probe_business.txt）——判据 11 不适用。
E 组（预期合格，防误伤-结构豁免）：研究语域且"结论先说+有限回扣"结构
  （tests/register_probe_summary.txt）——判据 10 豁免生效。
真实 LLM（purpose=agent.audit，temperature=0.0），每组 2 次。
判定：A ≥1 reject（判据 11）· C ≥1 reject（判据 10）· B/D/E 0 reject（未误伤）。

Run: cd backend && ../.venv/bin/python tests/probe_register_audit_20260921.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.gateway import get_model_gateway  # noqa: E402
from app.services.agent_loop import AgentLoop  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
USER_MSG = open(os.path.join(_REPO, "tests", "register_probe_user_msg.txt"), encoding="utf-8").read().strip()
OFFENDING = open(os.path.join(_REPO, "tests", "register_probe_offending.txt"), encoding="utf-8").read().strip()
CLEAN = open(os.path.join(_REPO, "tests", "register_probe_clean.txt"), encoding="utf-8").read().strip()
REPETITIVE = open(os.path.join(_REPO, "tests", "register_probe_repetitive.txt"), encoding="utf-8").read().strip()
BUSINESS = open(os.path.join(_REPO, "tests", "register_probe_business.txt"), encoding="utf-8").read().strip()
SUMMARY = open(os.path.join(_REPO, "tests", "register_probe_summary.txt"), encoding="utf-8").read().strip()

USER_MSG_BUSINESS = "帮我看下最近的服务器负载，用脚本统计一下，简单说说结论。"
# D 组夹具需带真实证据台账（否则会因「声称已执行但无工具结果」按编造被拒，
# 那是判据 4 的正确行为，不是判据 11 的语域误伤）
EVIDENCE_BUSINESS = (
    "<evidence-ledger>\n"
    "[工具结果 execute_code] stdout: mean_cpu=42.3%; peak=87.0% @ 2026-09-17 21:05; "
    "window=7d; script=load_stats.py\n"
    "</evidence-ledger>"
)

FOCUS = (
    "用户对当前学术稿件表达强烈不满，批评其只是重复和扩写论点、缺乏真实可信的学术思维、"
    "认为文章无价值；并追问表中大量标注的'受限估计/不可识别'究竟是什么意思、为何如此标注。"
)

assert len(OFFENDING) > 1000 and len(CLEAN) > 200, "夹具缺失"


def _extract_json(raw: str) -> dict:
    s = raw.find("{")
    e = raw.rfind("}") + 1
    if s < 0 or e <= s:
        return {}
    try:
        return json.loads(raw[s:e])
    except json.JSONDecodeError:
        return {}


async def audit(gw, draft: str, user_msg: str = USER_MSG, evidence: str = "") -> dict:
    system = AgentLoop._AUDITOR_SYSTEM_TEMPLATE.replace("__ASSISTANT_NAME__", "AI助手")
    context = (
        f"用户最新消息：{user_msg}\n\n"
        f"本轮意图（协调器判断）：{FOCUS}\n\n"
        f"助手草稿回答：{draft}"
    )
    if evidence:
        context += f"\n\n{evidence}"
    raw = await gw.chat(
        [{"role": "system", "content": system},
         {"role": "user", "content": context}],
        purpose="agent.audit", temperature=0.0,
    )
    js = _extract_json(raw)
    return {
        "verdict": js.get("verdict", ""),
        "ok": js.get("ok"),
        "problem": (js.get("problem") or "")[:200],
        "taxonomy": js.get("taxonomy", ""),
    }


async def main() -> int:
    gw = get_model_gateway()
    groups = {}
    cases = (
        ("A_offending", OFFENDING, USER_MSG, ""),
        ("B_clean", CLEAN, USER_MSG, ""),
        ("C_repetitive", REPETITIVE, USER_MSG, ""),
        ("D_business", BUSINESS, USER_MSG_BUSINESS, EVIDENCE_BUSINESS),
        ("E_summary", SUMMARY, USER_MSG, ""),
    )
    for name, draft, user_msg, evidence in cases:
        results = [await audit(gw, draft, user_msg, evidence) for _ in range(2)]
        groups[name] = results
        print("=" * 72)
        print(f"{name} ({len(draft)} chars)")
        for i, v in enumerate(results, 1):
            print(f"  [{i}] verdict={v['verdict']} ok={v['ok']} taxonomy={v['taxonomy']}")
            print(f"      problem={v['problem']}")

    a_reject = any(v["verdict"] == "reject" for v in groups["A_offending"])
    b_reject = any(v["verdict"] == "reject" for v in groups["B_clean"])
    c_reject = any(v["verdict"] == "reject" for v in groups["C_repetitive"])
    d_reject = any(v["verdict"] == "reject" for v in groups["D_business"])
    e_reject = any(v["verdict"] == "reject" for v in groups["E_summary"])
    ok = a_reject and c_reject and not (b_reject or d_reject or e_reject)
    print("=" * 72)
    print(f"VERDICT: A_offending_rejected={a_reject} · C_repetitive_rejected={c_reject} · "
          f"B_clean_rejected={b_reject} · D_business_rejected={d_reject} · "
          f"E_summary_rejected={e_reject} · PROBE {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
