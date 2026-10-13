"""RED tests (A4.8) — 新实体检索触发门（conv 0e8780e7, 2026-09-10）.

生产证据（服务器 <cloud-host>，conv 0e8780e7-04e5-4fa3-bd98-af5e6641aa56，
用户 魏洋，助手"默认助手"，模型 deepseek-v4-flash，2026-09-06 21:49 local）：
用户追问"那iqoo15ulra呢"，协调器 focus 已正确识别"用户现在询问 iQOO 15
Ultra 这款手机的情况"，却判 search_required=False（服务器日志实证）→
主模型零工具调用凭记忆断言"目前市面上并不存在一款确定的 iQOO 15 Ultra
机型"（DB msg 5c2cff4b results_n=0 实证）→ 草稿带"截至我的知识范围/
我可以帮你联网核实"措辞落入【诚实回答豁免】被 accept。用户下一轮纠正后
协调器判 search_required=True → 真实检索 → 正确答案（iQOO 15 Ultra
2026-02 已发布上市）。

根因（A4.6 三层缺口）：
  L1 协调器 search_required 三条件（明确要求搜索/时效性/冷僻具体事实）未
     覆盖"知识截止后的新实体/新品存在性咨询"——判定边界有洞；
  L2 审计【诚实回答豁免】边界过宽：草稿头部是对同一实体的具体存在性断言
     （"不存在"），不是纯粹的"我不知道"，仍被豁免；
  L3 审计零工具 (b) 口径祝福"沿用之前轮次引用编号 [N]"——与每轮独立台账
     机制矛盾（跨轮编号碰撞的审计侧源头，conv a040c24e）。

Required behavior:
  T1 协调器模板 search_required 增"新实体/存在性不确定 → true"条件，含
     禁止凭记忆断言实体存在或不存在的教训注记（conv 0e8780e7）。
  T2 审计模板【无证据高特异性事实声称】枚举覆盖实体存在性/发布状态断言
     （无论肯定或否定）。
  T3 审计模板【诚实回答豁免】明确边界：草稿对同一事物同时做出具体事实
     断言的部分不豁免。
  T4 审计零工具 (b) 口径不再祝福沿用历史引用编号——历史编号仅在其所属
     轮次有效，零工具轮带 [N] 引用按无法核实处理。

Run: cd backend && python -m pytest tests/test_new_entity_search_gate.py -v
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import AgentLoop


# ── T1: coordinator search_required covers new/unknown entities ──
def test_t1_coordinator_search_required_new_entity():
    t = AgentLoop._COORDINATOR_CLASSIFY_TEMPLATE
    # A fourth trigger condition for possibly-recent / unknown-existence entities.
    ok_entity = ("不确定" in t and "存在" in t) and ("新" in t)
    ok_lesson = "0e8780e7" in t or "凭记忆断言" in t or "断言其存在或不存在" in t
    assert ok_entity, "search_required 未覆盖'新实体/存在性不确定 → true'条件"
    assert ok_lesson, "search_required 缺少 conv 0e8780e7 教训注记（禁止凭记忆断言实体存在/不存在）"


# ── T2: auditor niche-claim rule covers existence/release assertions ──
def test_t2_auditor_niche_claim_covers_existence_assertion():
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    ok_existence = ("存在性" in t) or ("发布状态" in t) or ("上市状态" in t)
    assert ok_existence, "审计'无证据高特异性事实声称'枚举未覆盖实体存在性/发布状态断言"


# ── T3: honesty exemption bounded against concurrent factual assertions ──
def test_t3_honesty_exemption_boundary():
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    # The exemption must say: drafts that ALSO make concrete factual assertions
    # about the same entity are NOT covered by the honesty pass.
    ok_boundary = ("不豁免" in t or "不适用" in t or "不等于" in t) and ("具体" in t and "断言" in t)
    assert ok_boundary, "诚实回答豁免缺少边界：对同一事物的具体事实断言部分不豁免"


# ── T4: zero-tool (b) clause no longer blesses stale citation numbers ──
def test_t4_zero_tool_b_clause_stale_citation():
    import asyncio

    class _Cap:
        def __init__(self):
            self.calls = []

        async def __call__(self, messages, **kw):
            self.calls.append(list(messages))
            return '{"ok": true, "problem": ""}'

    from tests.test_factual_grounding_gate import make_loop  # noqa: E402

    cap = _Cap()
    loop = make_loop([])
    loop.auditor = cap
    loop._complete_json = cap
    from app.services.agent_loop import AgentLoopState

    state = AgentLoopState(messages=[{"role": "user", "content": "那iqoo15ultra呢"}])
    state.turn_focus = "询问 iQOO 15 Ultra"
    asyncio.run(loop._audit_response(state, "据了解 iQOO 15 Ultra 不存在 [1]。", "那iqoo15ultra呢"))
    assert cap.calls, "auditor LLM was never called"
    user_msg = cap.calls[0][1]["content"]
    assert "本轮实际调用工具：无" in user_msg, "zero-tool line missing"
    # (b) must NOT present stale-number reuse as acceptable; it must state
    # historical numbers are only valid in their own turn.
    ok_b = ("所属轮次" in user_msg or "仅在其" in user_msg or "不得沿用" in user_msg) and (
        "编号" in user_msg
    )
    assert ok_b, f"零工具 (b) 口径未声明历史引用编号仅在其所属轮次有效 | line={user_msg[:400]}"


# ── T5: auditor clause 4 must not bless stale-number reuse either (A4.9 R1 I1) ──
def test_t5_clause4_no_stale_number_blessing():
    """A4.9 R1 Important：审计合格标准第 4 条仍保留'（沿用其引用编号且内容一致）'
    的祝福——与改写后的 (b) 口径直接矛盾（同一零工具沿用旧编号草稿，第4条 accept、
    (b) 无法核实）。必须对齐：基于历史结果内容作答可以，沿用历史 [N] 编号不行。"""
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    assert "沿用其引用编号" not in t, "第 4 条仍祝福'沿用其引用编号'（与 (b) 口径矛盾）"
    # 对齐后的条款仍需保留"基于历史结果内容作答合格"的正面口径
    ok_positive = "对话历史中已有的真实检索结果" in t
    assert ok_positive, "第 4 条正面口径（基于历史结果内容作答合格）被误删"


# ── T6: system prompt citation rule scopes numbering to the CURRENT turn ──
def test_t6_system_prompt_citation_numbering_scope():
    """部署后冒烟（conv 2d6ff3a7，2026-09-10）：规则 7"多轮搜索共享同一套全局
    编号"被模型误读为跨对话轮次连续编号——turn2 草稿凭推算幻觉 [16][17]
    （turn1 10 条+本轮第 6/7 条），审计两连拒后模型放弃全部引用。必须明确：
    编号仅在同一轮对话内延续，跨轮各自独立、各从 1 开始，禁止凭推算使用
    历史轮次编号。"""
    import asyncio
    from types import SimpleNamespace

    from app.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    shared = SimpleNamespace(memory_summary="", dream_summary="", memory_entries=[])
    workspace = SimpleNamespace(root_path="/tmp/x")
    prompt = asyncio.run(
        svc._build_system_prompt(assistant=None, shared_context=shared, workspace=workspace, user=None)
    )
    ok_scope = "轮次" in prompt and "各从 1" in prompt
    ok_no_guess = "推算" in prompt or "历史轮次的编号" in prompt
    assert ok_scope, "规则 7 未明确编号跨对话轮次独立、各从 1 开始"
    assert ok_no_guess, "规则 7 未禁止凭记忆/推算使用历史轮次编号"


# ── T7: audit context carries the valid citation-id range (leftover ②) ──
def test_t7_auditor_template_valid_range_instruction():
    """冒烟观察（conv 2d6ff3a7）：写手因引用编号两连拒后放弃全部引用——
    审计模板必须要求：因引用编号打回时 problem 写明本轮有效编号范围，
    引导写手修正引用而非弃引。"""
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    ok = ("有效" in t and "编号" in t and "范围" in t) and ("放弃引用" in t or "弃引" in t or "而非" in t)
    assert ok, "审计模板缺少'引用编号打回须写明本轮有效编号范围'指令"


def test_t7_audit_context_injects_valid_range():
    """有检索轮次的审计上下文必须注入本轮有效引用编号范围 1..N。"""
    import asyncio
    from types import SimpleNamespace

    from app.services.agent_loop import AgentLoopState
    from app.services.citation_ledger import CitationLedger
    from tests.test_factual_grounding_gate import make_loop

    class _Cap:
        def __init__(self):
            self.calls = []

        async def __call__(self, messages, **kw):
            self.calls.append(list(messages))
            return '{"ok": true, "problem": ""}'

    ledger = CitationLedger()
    for i in range(3):
        ledger.register(f"http://e{i}.example/", title=f"t{i}", snippet="s")

    cap = _Cap()
    loop = make_loop([])
    loop.auditor = cap
    loop._complete_json = cap
    state = AgentLoopState(messages=[{"role": "user", "content": "对比一下"}])
    state.tool_results = [
        SimpleNamespace(
            name="web_search",
            result=json.dumps({"count": 3, "results": [], "formatted": ""}, ensure_ascii=False),
            error=False,
            arguments={"queries": ["q"]},
            call_id="c1",
        )
    ]
    state.citation_ledger = ledger
    asyncio.run(loop._audit_response(state, "答案 [1] 与 [9]。", "对比一下"))
    assert cap.calls, "auditor LLM was never called"
    user_msg = cap.calls[0][1]["content"]
    assert "[1]-[3]" in user_msg or "1]-[3" in user_msg or ("有效" in user_msg and "3" in user_msg and "编号" in user_msg), \
        f"审计上下文未注入有效引用编号范围 | {user_msg[:400]}"


# ── T8: auditor flags zero-citation drafts that substantially use search results ──
def test_t8_auditor_zero_citation_clause():
    """生产冒烟观察（2026-09-10，两次）：历史中性化后模型上下文再无引用示例，
    有检索轮次草稿实质使用检索结果的具体信息却零 [N] 标记——引用卡片静默消失。
    审计模板必须覆盖：有检索+实质使用+零引用 → needs_evidence（软拒），
    problem 写明有效编号范围，要求为关键事实补标来源，而非整段重写。"""
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    ok_clause = ("实质使用" in t) and ("没有任何 [N]" in t or "零 [N]" in t) and "needs_evidence" in t
    ok_guidance = "补标" in t
    assert ok_clause, "审计模板缺少'有检索+实质使用+零引用 → needs_evidence'条款"
    assert ok_guidance, "审计模板缺少'为关键事实补标来源（而非整段重写）'的引导"
