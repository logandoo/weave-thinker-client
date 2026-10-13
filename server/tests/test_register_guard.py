"""TDD: 领域语域守护 — 学术/研究任务不得用工程流程语域；单条草稿内部不得语义复述。

用户两问（conv a104fbc5 末答，2026-09-21）：
  ①「回答本身是车轱辘话，而且语句不通顺」
  ②「对学术文章的定义，应该是假设，验证，结论，怎么现在 agent 写的更像是 coding 的流程？」
根因（docs/AUDIT_20260921_a104_answer_register.md）：
  提示词零研究法脚手架（agent_service.py"论文/假说"=0）+ 死磕机制工程交付语
  （deathmatch_service.py:1178"受限估计"占位词）+ 审计无"内部重述/领域语域"判据
  （agent_loop.py:4813-4850）。
本波 R1–R4：
  R1 系统提示词全局语域段（研究链：问题→假说/命题→数据与识别→证据→结论与局限）
  R2 死磕计划/重规划语域纪律（研究类目标不得用工程流程词、不得用状态占位词）
  R3 发送前审计新增两条判据（草稿内部语义复述；学术/研究类任务领域语域）
  R4 用户可见文本禁内部流程词（审计/质检/返工/闸门/占位/降级协议）

Run: cd backend && ../.venv/bin/python -m pytest tests/test_register_guard.py -v
"""
import asyncio
from types import SimpleNamespace

from app.services.agent_service import AgentService
from app.services.agent_loop import AgentLoop
from app.services.deathmatch_service import DeathmatchManager
from app.services.memory_service import AgentSharedContext


def _build_prompt(deathmatch_mode: bool = False) -> str:
    service = AgentService()
    shared = AgentSharedContext(
        agent_state=None,
        memory_summary="",
        dream_summary="",
        memory_entries=[],
    )
    workspace = SimpleNamespace(root_path="/tmp/wt-register-test")
    return asyncio.run(
        service._build_system_prompt(
            assistant=None,
            shared_context=shared,
            workspace=workspace,
            user=None,
            identity_context="",
            deathmatch_mode=deathmatch_mode,
        )
    )


class TestRegisterSectionR1R4:
    """R1/R4: 全局表达语域段（两模式都要有）。"""

    def test_register_section_present_in_normal_mode(self):
        prompt = _build_prompt(deathmatch_mode=False)
        assert "表达语域与领域匹配" in prompt
        # 研究链（用户指定的 假设→验证→结论 的完整化）
        assert "假说/命题" in prompt
        assert "研究问题" in prompt
        assert "结论与局限" in prompt

    def test_register_section_present_in_deathmatch_mode(self):
        assert "表达语域与领域匹配" in _build_prompt(deathmatch_mode=True)

    def test_engineering_words_banned_for_research_register(self):
        prompt = _build_prompt()
        # 研究活动不得用工程流程词（给出替换映射）
        for banned in ("降级", "返工", "填报", "闭环"):
            assert banned in prompt, f"替换映射缺少 {banned}"
        assert "研究语域" in prompt

    def test_research_gap_expression_by_convention(self):
        prompt = _build_prompt()
        # 无法给出的结果按研究惯例如实表述；自造状态标签被点名
        assert "未估计" in prompt
        assert "未报告" in prompt
        assert "识别边界" in prompt
        assert "受限估计" in prompt  # 作为被禁的自造标签示例

    def test_research_gap_requires_objective_unavailability(self):
        # A4.9 r1 F1：正向措辞必须有前提，防「可执行未执行」改写成研究语域规避
        prompt = _build_prompt()
        assert "已尝试获取" in prompt
        assert "严禁把可执行而未执行的工作改写成" in prompt
        assert "不得把可执行而未执行的工作改写成上述措辞" in _build_prompt(deathmatch_mode=True)

    def test_internal_process_words_banned_in_user_visible_text(self):
        prompt = _build_prompt()
        assert "不得在用户可见文本中暴露内部" in prompt
        assert "闸门" in prompt
        assert "占位" in prompt
        assert "降级协议" in prompt

    def test_deathmatch_register_section_only_in_deathmatch_mode(self):
        assert "研究/学术类目标的语域纪律" not in _build_prompt(deathmatch_mode=False)
        assert "研究/学术类目标的语域纪律" in _build_prompt(deathmatch_mode=True)


class TestPlanPromptRegisterR2:
    """R2: 死磕计划/重规划提示词的语域纪律。"""

    def test_plan_prompt_has_register_clause(self):
        p = DeathmatchManager.PLAN_SYSTEM_PROMPT
        assert "研究/学术类目标" in p
        assert "工程/办公流程词" in p
        assert "待执行" in p and "受限估计" in p
        # A4.9 r1 F6：正向写法 + 字段边界
        assert "正向写法" in p
        assert "人类可读字段" in p

    def test_replanner_prompt_has_register_clause(self):
        p = DeathmatchManager.REPLANNER_SYSTEM_PROMPT
        assert "研究/学术类目标" in p
        assert "工程/办公流程词" in p
        assert "正向写法" in p
        assert "verification_method" in p

    def test_placeholder_judge_prompt_blocks_research_register_evasion(self):
        # A4.9 r1 F1：judge 端硬化——换词不改变未执行事实
        from app.services.deathmatch_service import _PLACEHOLDER_JUDGE_PROMPT
        assert "换词不改变未执行事实" in _PLACEHOLDER_JUDGE_PROMPT
        assert "未估计" in _PLACEHOLDER_JUDGE_PROMPT
        assert "客观不可得且已尝试获取" in _PLACEHOLDER_JUDGE_PROMPT


class TestAuditorCriteriaR3:
    """R3: 发送前审计新增判据（内部语义复述 + 领域语域）。"""

    def test_auditor_has_intra_draft_repetition_criterion(self):
        tpl = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
        assert "草稿内部不得语义复述" in tpl
        assert "车轱辘" in tpl
        # A4.9 r1 F5：结构化/导航性回扣豁免（防误伤摘要+正文）
        assert "豁免" in tpl
        # A4.9 r2：可操作阈值（同一内容≥3 处且无新信息）
        assert "3 处及以上" in tpl

    def test_auditor_has_domain_register_criterion(self):
        tpl = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
        assert "领域语域检查" in tpl
        assert "学术/研究/写作类" in tpl
        assert "未估计" in tpl
        assert "受限估计" in tpl
        # 工程任务不受此条约束（防误伤）
        assert "软件/工程类任务不适用" in tpl
        # A4.9 r1 F2：显式排除数据分析/运维类 + 引用豁免
        assert "运维类问答不适用" in tpl
        assert "引用用户原词而提及上述词汇不算违规" in tpl

    def test_auditor_repetition_criterion_is_intra_draft_scoped(self):
        tpl = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
        assert "单条草稿内部" in tpl
