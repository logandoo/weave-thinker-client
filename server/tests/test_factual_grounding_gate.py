"""RED tests (A4.8) — 无搜索事实轮幻觉四层修复 (conv 8074c1c5, 2026-08-29).

生产证据（服务器 <cloud-host>，conv 8074c1c5-a322-4c61-a566-d82a0386d41a，
用户 YearDoo，助手"研学策划"，模型 deepseek-v4-flash，2026-06-19→2026-08-28）：
25 轮回答中 16 轮零工具调用；全部被用户实锤的幻觉（"武定门原名长乐门"、
"老虎头=王导请高僧降虎化石"整段编造传说、九龙桥细节、"红石砌进城墙"）
全部发生在未搜索回合；凡真实 web_search 的回合答案基本有据（[N] 引用
对应真实检索结果）。用户两轮现场纠偏（#41/#47/#49）助手均承认编造。

根因（A4.6 四层缺口）：
  L1 协调器 search_required 定义只覆盖"用户明确要求搜索"∪"时效性信息"，
     静态冷僻史实问题（地名典故）→ false → 无 search-demand 指令、审计预算降 2；
  L2 是否搜索全凭主模型自觉，flash 级模型长对话惯性下凭参数记忆作答；
  L3 审计模板对零工具知识型回答只有"谎称搜索/假引用编号"可拒——编造的
     典故不带假引用，auditor 无理由拒，且 auditor 同为 flash 模型共享同一
     错误参数记忆（自评盲）；
  L4 creative_turn 豁免可被"给孩子讲故事"句式命中 → 审计整段跳过。

Required behavior:
  T1  协调器模板 search_required 覆盖"冷僻具体事实"域（历史典故/地名由来/
      人物事件/具体日期数字），并明确常识通识除外（防过触发）。
  T2  协调器模板 creative_turn 排除"以讲故事形式讲解真实事实"——事实性
      内容必须走正常审计；纯虚构文学创作仍豁免。
  T3  审计模板含"无证据高特异性事实声称"条款：台账为空 + 冷僻具体事实
      声称 → needs_evidence；常识通识放行；禁止审计员以自身记忆作依据。
  T4  审计零工具上下文行含 (d) 口径（_audit_response 注入文本实证）。
  T5  系统提示"主动求知"含冷僻史实/典故必须先搜索核实条款
      （_build_system_prompt 输出实证）。

Run: cd backend && python -m pytest tests/test_factual_grounding_gate.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import AgentLoop, AgentLoopState


# ── minimal harness (mirror tests/test_turn_boundary_directives.py pattern) ──
class ScriptedAuditor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        if self.results:
            return self.results.pop(0)
        return '{"ok": true, "problem": ""}'


ACCEPT = '{"ok": true, "problem": ""}'


class _DigestCfg:
    enabled = False


def make_loop(auditor_results):
    loop = AgentLoop.__new__(AgentLoop)
    loop.llm = None
    loop.iteration_llm = None
    loop.provider_type = "deepseek"
    loop.iteration_provider_type = "deepseek"
    loop.iteration_extra_body_override = None
    loop.enable_reasoning = False
    loop.reasoning_effort = None
    loop.thinking_budget = None
    loop.deathmatch_manager = None
    loop._compressor = None
    loop.enable_compression = False
    loop._canary_enabled = False
    loop._canary_marker = None
    loop.max_iterations = 30
    loop.tool_schemas = []
    loop._active_tool_names = set()
    loop.session_factory = None
    loop._tool_call_timeout = 60
    loop._digest_config = _DigestCfg()
    loop.workspace_path = None
    loop.auditor = ScriptedAuditor(auditor_results)
    loop._complete_json = loop.auditor
    loop._try_fallback_provider = lambda: False
    return loop


NICHE_FACT_DRAFT = (
    "武定门原名长乐门，1935年民国二十四年增辟。当年守城将士在门下埋了二十四口"
    "大缸用于防火，每逢端午要举行祭门仪式，这个习俗一直延续到1937年。"
)


# ── T1: coordinator search_required covers niche-fact grounding ──
def test_t1_coordinator_search_required_niche_facts():
    t = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
    ok_niche = ("历史典故" in t or "地名由来" in t) and "冷僻" in t
    ok_exclusion = "常识" in t  # common-knowledge exclusion prevents over-trigger
    assert ok_niche, "search_required 判定未覆盖冷僻具体事实域（历史典故/地名由来）"
    assert ok_exclusion, "search_required 缺少常识通识除外条款（防过触发）"


# ── T2: creative_turn excludes fact-based storytelling ──
def test_t2_creative_turn_excludes_factual_storytelling():
    t = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
    # The creative_turn block must state that fact-based explanation (even in
    # story form / for children) is NOT creative writing and stays audited.
    ok = ("事实" in t and ("讲故事" in t or "给孩子" in t)) and (
        "不是创意写作" in t or "不属于创意写作" in t or "creative_turn: false" in t
    )
    assert ok, "creative_turn 定义未排除'以讲故事形式讲解真实事实'的轮次"


# ── T3: auditor template has zero-evidence niche-claim rule ──
def test_t3_auditor_template_niche_claim_rule():
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    ok_rule = ("高特异性" in t or "冷僻" in t) and "needs_evidence" in t
    ok_no_self_memory = "自身记忆" in t or "自己的记忆" in t or "自身的记忆" in t
    assert ok_rule, "审计模板缺少'无证据冷僻具体事实声称 → needs_evidence'条款"
    assert ok_no_self_memory, "审计模板未禁止审计员以自身记忆作判定依据（自评盲）"


# ── T4: zero-tool audit context carries the (d) clause ──
def test_t4_zero_tool_context_d_clause():
    loop = make_loop(auditor_results=[ACCEPT])
    state = AgentLoopState(messages=[{"role": "user", "content": "南京武定门有什么历史？"}])
    state.turn_focus = "南京地方史咨询"
    asyncio.run(loop._audit_response(state, NICHE_FACT_DRAFT, "南京武定门有什么历史？"))
    assert loop.auditor.calls, "auditor LLM was never called"
    user_msg = loop.auditor.calls[0][1]["content"]
    assert "本轮实际调用工具：无" in user_msg, "zero-tool line missing"
    ok_d = "(d)" in user_msg and ("冷僻" in user_msg or "高特异性" in user_msg) and (
        "needs_evidence" in user_msg or "按无法核实处理" in user_msg or "核实" in user_msg
    )
    assert ok_d, f"零工具上下文缺少 (d) 冷僻事实口径 | line={user_msg[:300]}"


# ── T5: system prompt requires search for niche historical facts ──
def test_t5_system_prompt_niche_fact_search():
    from types import SimpleNamespace

    from app.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    shared = SimpleNamespace(memory_summary="", dream_summary="", memory_entries=[])
    workspace = SimpleNamespace(root_path="/tmp/x")
    prompt = asyncio.run(
        svc._build_system_prompt(assistant=None, shared_context=shared, workspace=workspace, user=None)
    )
    ok = ("典故" in prompt or "史实" in prompt or "地名由来" in prompt) and "冷僻" in prompt
    assert ok, "系统提示'主动求知'缺少冷僻史实/典故必须先搜索核实条款"
