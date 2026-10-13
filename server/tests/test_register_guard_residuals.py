"""TDD: 语域守护残留项收口（A4.9 r2 Minor M1/M4/M5）。

- M1/M5：占位判定 judge 只收交付物摘录 → 无法判定"本应执行 vs 客观不可得"。
  修复 = 注入【任务要求上下文】（目标 + 验收义务文本 + 相关步骤承诺/证据契约），
  并以"上下文+摘录"为缓存键；附行为探针（probe_placeholder_judge_context_20260921.py）。
- M4：语域/复述类审计拒属 `other` 族（免 stall cut）→ 新增 `style` 族（不豁免）。

Run: cd backend && ../.venv/bin/python -m pytest tests/test_register_guard_residuals.py -v
"""
from app.services.agent_loop import _audit_family_stalled, _audit_problem_family
from app.services.deathmatch_service import (
    _PLACEHOLDER_JUDGE_PROMPT,
    _build_placeholder_judge_content,
    _placeholder_judge_obligation_context,
)

GOAL = "写一篇论文并跑出回归系数"
OBLIGATION = "交付物不得以未执行占位（待执行/受限估计/待填报/不填报数值等）代替实际结果"
STEPS = [
    {
        "id": "s5",
        "description": "实证分析",
        "expected_output": "包含系数与标准误的结果表",
        "evidence_spec": {
            "kind": "compute",
            "must_run": True,
            "receipt": {
                "cmds": ["python run.py"],
                "outputs": ["results.json"],
                "min_rows": 1,
            },
        },
    },
    {
        "id": "s8",
        "description": "合成论文",
        "expected_output": "paper.md",
    },
]


class TestAuditProblemFamilyStyle:
    """M4: 语域/复述类拒可归族 → 同族 stall cut 生效。"""

    def test_repetition_problem_classified_as_style(self):
        assert _audit_problem_family("llm", "草稿内部语义复述：应合并为一处陈述") == "style"

    def test_register_problem_classified_as_style(self):
        assert _audit_problem_family("llm", "研究语域不合规：用工程流程词指代研究活动") == "style"
        assert _audit_problem_family("llm", "车轱辘话，同一结论重复展开") == "style"

    def test_existing_families_unchanged(self):
        assert _audit_problem_family("llm", "与证据台账矛盾") == "contradiction"
        assert _audit_problem_family("llm", "凭空编造工具结果") == "unsupported"
        assert _audit_problem_family("npg", "数值无回执") == "npg"
        assert _audit_problem_family("llm", "无法归类的问题") == "other"

    def test_style_family_is_not_stall_exempt(self):
        assert _audit_family_stalled(["style", "style", "style"], 3) is True
        assert _audit_family_stalled(["other", "other", "other"], 3) is False
        assert _audit_family_stalled(["npg", "npg", "npg"], 3) is False


class TestPlaceholderJudgeContext:
    """M1/M5: judge 收到任务要求上下文（有界），缓存键随上下文变化。"""

    def test_content_includes_context_and_excerpt(self):
        content = _build_placeholder_judge_content("EXCERPT-BODY", "CTX-BODY")
        assert "EXCERPT-BODY" in content
        assert "CTX-BODY" in content
        assert "任务要求上下文" in content
        # 无上下文时向后兼容
        content2 = _build_placeholder_judge_content("EXCERPT-BODY", "")
        assert "EXCERPT-BODY" in content2
        assert "任务要求上下文" not in content2

    def test_context_builder_extracts_goal_obligation_and_step_commitments(self):
        ctx = _placeholder_judge_obligation_context(
            goal=GOAL,
            criteria_texts=[OBLIGATION],
            steps=STEPS,
            files=["results.json"],
        )
        assert GOAL[:50] in ctx
        assert "未执行占位" in ctx
        assert "s5" in ctx
        assert "系数" in ctx
        assert "python run.py" in ctx
        # 与 files 无关的步骤（无声明输出匹配）不进入上下文
        assert "s8" not in ctx

    def test_context_builder_bounded(self):
        ctx = _placeholder_judge_obligation_context(
            goal="G" * 5000,
            criteria_texts=["C" * 3000, "D" * 3000],
            steps=[{"id": "s1", "description": "D" * 5000, "expected_output": "E" * 5000}],
            files=["out.md"],
            limit=1200,
        )
        assert 0 < len(ctx) <= 1200

    def test_context_builder_marks_truncation(self):
        # A4.9 r3 F7：上下文截断必须显式披露
        ctx = _placeholder_judge_obligation_context(
            goal="G" * 5000,
            criteria_texts=["C" * 500, "D" * 500, "E" * 500],
            steps=[],
            files=[],
            limit=1200,
        )
        assert "上下文超限截断" in ctx
        assert len(ctx) <= 1200

    def test_context_builder_falls_back_to_must_run_on_path_mismatch(self):
        # A4.9 r3 F3：路径词法不匹配（./ 前缀）不得丢掉"本应执行"依据
        ctx = _placeholder_judge_obligation_context(
            goal="G",
            criteria_texts=["C"],
            steps=[{
                "id": "s5",
                "description": "估计",
                "expected_output": "paper.md",
                "evidence_spec": {
                    "must_run": True,
                    "receipt": {"cmds": ["python run.py"], "outputs": ["./paper.md"]},
                },
            }],
            files=["paper.md"],
        )
        assert "must_run" in ctx
        assert "python run.py" in ctx

    def test_context_builder_fallback_branch_when_no_path_matches_at_all(self):
        # A4.9 r4 N2：真正进入兜底分支（归一后仍无交集）——声明输出在子目录
        ctx = _placeholder_judge_obligation_context(
            goal="G",
            criteria_texts=["C"],
            steps=[
                {
                    "id": "s5",
                    "description": "估计",
                    "expected_output": "results.json",
                    "evidence_spec": {
                        "must_run": True,
                        "receipt": {"cmds": ["python run.py"], "outputs": ["analysis/results.json"]},
                    },
                },
                {
                    "id": "s8",
                    "description": "合成",
                    "expected_output": "paper.md",
                    "output_files": ["analysis/report.md"],
                },
            ],
            files=["paper.md"],
        )
        assert "must_run" in ctx
        assert "python run.py" in ctx
        # 未匹配的非 must_run 步骤仍不进入上下文
        assert "合成" not in ctx

    def test_judge_prompt_requires_context_comparison(self):
        assert "任务要求上下文" in _PLACEHOLDER_JUDGE_PROMPT
        assert "本应实际执行" in _PLACEHOLDER_JUDGE_PROMPT
        # 既有硬化句保留
        assert "换词不改变未执行事实" in _PLACEHOLDER_JUDGE_PROMPT
        assert "客观不可得且已尝试获取" in _PLACEHOLDER_JUDGE_PROMPT
