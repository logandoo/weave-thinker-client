"""TDD tests: 死磕 W1a — 执行义务基线 + 占位阻断 + evidence_spec 冻结。

背景：conv a104fbc5 审计（docs/AUDIT_20260921_a104_deathmatch_paper.md）+
设计（docs/DEATHMATCH_EXECUTION_CONTRACT_DESIGN.md §5.3/§12）。

r5/r6（用户原则：语义判断必须 agentic，禁止关键词枚举）：
- 是否要求真实执行 → 由 agentic 判定缓存 `_ensure_execution_judged` 决定；
  不再用 `_EMPIRICAL_HINTS`/数值正则做目标分类（M3 的 expected_output 契约
  字段检查除外——那是契约字段，不是意图分类）。
- 降级留白授权 = 结构化契约字段（W1b），本波 `allow=False` 恒定。
- 义务在计划定稿时持久化（append-only）；markers 判据路径在检查时按活计划
  解析（解决“产出文件完成时才回填”导致的闸门不武装）。
- 在途（非本波创建、从未 apply）会话不追溯（I4）；M3 仅作用于新步骤。

本文件按 A4.8 先行：实现前运行必须 RED。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_deathmatch_obligations.py -v
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.deathmatch_service as ds
from app.services.deathmatch_service import DeathmatchManager


# ── fixtures ──────────────────────────────────────────────────────────────

_EMPIRICAL_GOAL = (
    "目标描述：中国货币循环与“钱去哪了”宏观分析论文——采用计量回归与因果推断，"
    "使用官方统计数据交叉验证，重点 2022—2025 年。"
)
_EMPIRICAL_STEP = {
    "id": "s5",
    "kind": "write",
    "description": "撰写实证分析章节",
    "expected_output": "文件 empirical.md，含回归表系数、标准误、显著性、观测数",
    "output_files": ["empirical.md"],
    "status": "in_progress",
}
_PLACEHOLDER_MD = (
    "# 第6章 实证分析\n\n"
    "表6-2 基准回归设定与执行状态：受限估计：不填报数值。\n"
    "表6-4 稳健性检验方案：受限估计：待执行。\n"
)
_CLEAN_MD = (
    "# 第6章 实证分析\n\n"
    "基准回归结果：核心解释变量系数 0.123（标准误 0.031），N=1042，R2=0.41。\n"
)


def test_read_only_boundary_suppresses_contradictory_file_obligations(tmp_path):
    read_only = {
        "id": "s2",
        "description": "只读回读目标文件",
        "kind": "write",
        "boundary": "只读核验，不得创建任何核验中间文件",
        "expected_output": "对话内回读结论",
        "verification_method": "使用工具回读",
        "done_check": {"mode": "file", "path": "verify.json", "min_bytes": 1},
        "output_files": [],
        "writes": [],
        "evidence_spec": {"kind": "compute", "receipt": {"cmds": ["python verify.py"], "outputs": ["/tmp/verify.json"]}},
    }
    assert ds._declared_output_paths(read_only) == [], "只读边界必须优先于计划器自洽失败的文件声明"
    criteria = ds._build_obligation_criteria([read_only], {"s2"})
    assert [c["id"] for c in criteria] == ["ob1"], "ob1 保留，但 tool-output 不得再派生 ob2 文件义务"

    compute_step = dict(read_only, boundary="", evidence_spec={"kind": "compute", "receipt": {"cmds": ["python compute.py"], "outputs": ["verify.json"]}})
    assert ds._declared_output_paths(compute_step), "非只读计算步骤仍保留真实执行凭证要求"
    assert len(ds._build_obligation_criteria([compute_step], {"s2"})) > 1

    scoped_read_only = dict(read_only, boundary="只读分析，严禁修改 config.toml", writes=["report.md"])
    assert "report.md" in ds._declared_output_paths(scoped_read_only), " scoped 只读不得误伤合法 report.md"
    assert len(ds._build_obligation_criteria([scoped_read_only], {"s2"})) > 1
    scoped_no_temp = dict(read_only, boundary="禁止生成临时核验文件；最终交付 report.md", writes=["report.md"])
    assert "report.md" in ds._declared_output_paths(scoped_no_temp)
    sanitized_scoped = ds._sanitize_step_file_contract(scoped_no_temp)
    assert sanitized_scoped["writes"] == ["report.md"], "禁止临时文件不得清除显式 report.md"
    explicit_allow = dict(read_only, boundary="仅允许创建并写入 report.md；不得创建任何其他文件", output_files=["report.md"], writes=["report.md"])
    assert "report.md" in ds._declared_output_paths(explicit_allow)
    comma_allow = dict(read_only, boundary="不得创建任何其他文件，但允许创建 report.md", output_files=["report.md"], writes=["report.md"])
    assert "report.md" in ds._declared_output_paths(comma_allow), "中文逗号不得误删明确允许的交付物"
    assert ds._sanitize_step_file_contract(explicit_allow)["output_files"] == ["report.md"]
    prohibited = dict(read_only, boundary="不得交付 report.md", output_files=["report.md"], writes=["report.md"])
    assert "report.md" not in ds._declared_output_paths(prohibited), "禁止交付不得被误判为允许交付"
    later_prohibited = dict(read_only, boundary="允许交付 report.md；不得交付 report.md", output_files=["report.md"], writes=["report.md"])
    assert "report.md" not in ds._declared_output_paths(later_prohibited), "后出现的禁止必须否决先前的允许"
    blanket_prohibited = dict(read_only, boundary="允许创建 report.md；不得创建任何文件", output_files=["report.md"], writes=["report.md"])
    assert "report.md" not in ds._declared_output_paths(blanket_prohibited), "全量禁止不得被跳过"
    strict = dict(read_only, output_files=["verify.json"], writes=["verify.json"])
    strict_sanitized = ds._sanitize_step_file_contract(strict)
    assert strict_sanitized["output_files"] == [] and strict_sanitized["writes"] == []
    assert strict_sanitized["done_check"] == {"mode": "none"}, "严格禁建不得保留被禁止的 file done_check"
    assert ds._declared_output_paths(strict) == []
    older = dict(scoped_read_only, output_files=["old.md"], status="done")
    changed = dict(older, output_files=["new.md"])
    assert ds.step_statement_hash(older) != ds.step_statement_hash(changed), "output_files 必须进入 settled 不可变哈希"
    assert ds._validate_done_steps_preserved([older], [changed]), "replan 不得替换已结算交付物"

    sanitized = ds._sanitize_step_file_contract(read_only)
    assert sanitized["kind"] == read_only["kind"], "严格禁建也不得改写原步骤语义"
    assert sanitized["expected_output"] == read_only["expected_output"]
    assert sanitized["verification_method"] == read_only["verification_method"]
    assert sanitized["evidence_spec"]["kind"] == "tool_output"
    assert sanitized["evidence_spec"]["receipt"]["outputs"] == []
    assert sanitized["done_check"] == {"mode": "none"}, "严格禁建时不得保留矛盾 file done_check"
    assert ds._validate_plan_protocol({"steps": [sanitized]}) == []

    conv = _conv(deathmatch_plan={"steps": [sanitized]})
    conv.deathmatch_acceptance_criteria = criteria
    issues = asyncio.run(DeathmatchManager(conv)._run_goal_comparator(str(tmp_path)))
    assert issues == [], f"tool-output 无文件核验不得被 ob1 反 block：{issues}"

    mixed_step = dict(read_only, evidence_spec={"kind": "compute", "receipt": {"cmds": ["python compute.py"], "outputs": []}})
    conv_mixed = _conv(deathmatch_plan={"steps": [sanitized, mixed_step]})
    conv_mixed.deathmatch_acceptance_criteria = criteria
    mixed_issues = asyncio.run(DeathmatchManager(conv_mixed)._run_goal_comparator(str(tmp_path)))
    assert mixed_issues, "混合 compute/tool_output 计划不得被单个 tool_output 免除 ob1"

    invalid = dict(sanitized, evidence_spec={"kind": "tool_output", "receipt": {"cmds": ["python verify.py"], "outputs": ["verify.json"]}})
    assert ds._validate_plan_protocol({"steps": [invalid]}), "tool_output.outputs 非空必须被协议拒绝"
    assert ds._safe_workspace_path("<s1解析出的绝对路径>/dm.txt") == "", "占位路径不得成为机械交付门"


def test_placeholder_prompt_does_not_require_process_evidence_inside_artifact():
    from app.services.deathmatch_service import _PLACEHOLDER_JUDGE_PROMPT
    assert "不得把绝对路径、回读日志、命令输出" in _PLACEHOLDER_JUDGE_PROMPT
    assert "不得据此判占位" in _PLACEHOLDER_JUDGE_PROMPT


def test_criteria_and_planning_use_original_baseline_not_recommendation_background():
    original = "【原始用户要求（基线）】\n文件必须恰好 17 字节"
    conv = _conv(
        deathmatch_goal=original + "\n\n【实际盘问决策】\n推荐背景：改为 18 字节",
        deathmatch_grilling_qa_history=[{"round": 1, "qa_pairs": [
            {"question": "如何写？", "answer": "按推荐执行，保持原始要求"},
        ]}],
    )
    mgr = DeathmatchManager(conv)
    captured = {}

    async def fake_generate(llm, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        return '{"criteria": [{"text": "文件为17字节", "type": "judgmental", "check": {"kind": "none"}}]}'

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    asyncio.run(mgr._synthesize_acceptance_criteria())
    assert "17 字节" in captured["prompt"]
    assert "18 字节" not in captured["prompt"], "推荐背景不得污染机械验收标准"
    planning = mgr._build_plan_user_prompt()
    assert "17 字节" in planning
    assert "18 字节" not in planning
    from app.services.deathmatch_workspace import _goal_baseline_for_contract
    legacy = "【实际盘问决策】旧问答\n\n【原始用户要求（基线）】\n文件必须恰好 17 字节"
    assert "17 字节" in _goal_baseline_for_contract(legacy), "QA-first 旧目标不得丢原始基线"


def test_criteria_exact_gate_uses_program_bytes_and_repairs_conflicting_minimum(monkeypatch):
    monkeypatch.setitem(ds.config.deathmatch, "verify_command_gate_enabled", False)
    conv = _conv(deathmatch_goal="创建 target.txt，内容恰好为 ALPHA\\nBETA\\nGAMMA\\n")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {
            "id": "c1", "text": "target.txt 至少 18 字节", "type": "mechanical",
            "check": {"kind": "file", "path": "sub//target.txt", "min_bytes": 18},
        },
        {
            "id": "c2", "text": "target.txt 与精确三行内容一致", "type": "mechanical",
            "check": {
                "kind": "exact_file", "path": "sub/target.txt",
                "content": "ALPHA\nBETA\nGAMMA\n",
            },
        },
        {
            "id": "c3", "text": "不得改动另一个文件", "type": "mechanical",
            "check": {"kind": "file", "path": "other.txt", "min_bytes": 99},
        },
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert [c["id"] for c in crit] == ["c1", "c2", "c3"], "不得借正规化删除机械检查"
    assert crit[0]["check"]["min_bytes"] == 17, "命令门关闭时也须保留纠正后的机械文件检查"
    assert "18" not in crit[0]["text"] and "17" in crit[0]["text"]
    normalized = [e for e in conv.deathmatch_events or [] if e.get("type") == "criteria_normalized"]
    assert normalized and normalized[-1].get("exact_bytes") == 17, "字节数必须由程序从精确 printf 负载计算"
    assert normalized[-1].get("min_bytes") == 18


@pytest.mark.parametrize("cmd", [
    "printf 'A' | cmp -s - target.txt || true",
    "printf 'A' | cmp -s - target.txt | cat",
    "echo printf 'A' | cmp -s - target.txt",
    "printf \"$HOME\" | cmp -s - target.txt",
    "f=target.txt; printf 'A' | cmp -s - \"$f\"",
    "printf '\ud800' | cmp -s - target.txt",
])
def test_criteria_legacy_cmp_candidate_is_rejected_not_weakened(cmd):
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {"id": "c1", "text": "target 至少 18B", "type": "mechanical",
         "check": {"kind": "file", "path": "target.txt", "min_bytes": 18}},
        {"id": "c2", "text": "target 精确内容", "type": "mechanical",
         "check": {"kind": "gate", "cmd": cmd}},
        {"id": "c3", "text": "其他文件保留", "type": "mechanical",
         "check": {"kind": "file", "path": "other.txt", "min_bytes": 99}},
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert crit == [], "legacy cmp 候选必须整体重生成/失败，不得留下被削弱的残余合同"
    assert conv.deathmatch_acceptance_criteria is None
    assert any(e.get("type") == "criteria_rejected" for e in conv.deathmatch_events or [])


def test_criteria_any_legacy_gate_pauses_instead_of_persisting():
    conv = _conv(deathmatch_goal="保留其他文件检查")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {"id": "c1", "text": "other 至少 1B", "type": "mechanical",
         "check": {"kind": "file", "path": "other.txt", "min_bytes": 1}},
        {"id": "c2", "text": "target 精确内容", "type": "mechanical",
         "check": {"kind": "gate", "cmd": "test -f target.txt"}},
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert crit == [] and conv.deathmatch_status == "paused", "新 criteria 不再接受任何 legacy gate，包括非 cmp gate"


def test_criteria_structured_exact_content_handles_percent_deterministically():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {"id": "c1", "text": "target 至少 6B", "type": "mechanical",
         "check": {"kind": "file", "path": "target.txt", "min_bytes": 6}},
        {"id": "c2", "text": "target 精确内容", "type": "mechanical",
         "check": {"kind": "exact_file", "path": "target.txt", "content": "100%\n"}},
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert crit[0]["check"]["min_bytes"] == 5, "结构化 content 必须由 Python 按 UTF-8 实际字节计算"


def test_criteria_conflicting_exact_payloads_reject_entire_candidate():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {"id": "c1", "text": "精确 A", "type": "mechanical",
         "check": {"kind": "exact_file", "path": "target.txt", "content": "A"}},
        {"id": "c2", "text": "精确 BB", "type": "mechanical",
         "check": {"kind": "exact_file", "path": "./target.txt", "content": "BB"}},
        {"id": "c3", "text": "其他文件保留", "type": "mechanical",
         "check": {"kind": "file", "path": "other.txt", "min_bytes": 99}},
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert crit == [] and conv.deathmatch_acceptance_criteria is None, "同路径互斥精确内容必须拒绝整个候选，不能后写覆盖先写或保留残余"


def test_exact_file_comparator_always_checks_real_bytes(tmp_path, monkeypatch):
    monkeypatch.setitem(ds.config.deathmatch, "verify_command_gate_enabled", False)
    target = tmp_path / "target.txt"
    target.write_bytes(b"ALPHA\nBETA\nGAMMA\n")
    conv = _conv(deathmatch_acceptance_criteria=[{
        "id": "exact", "text": "精确 17B", "type": "mechanical", "source": "system",
        "check": {"kind": "exact_file", "path": "target.txt", "content": "ALPHA\nBETA\nGAMMA\n"},
    }])
    mgr = DeathmatchManager(conv)
    assert asyncio.run(mgr._run_goal_comparator(str(tmp_path))) == []
    target.write_bytes(b"ALPHA\nBETA\nGAMMA")
    issues = asyncio.run(mgr._run_goal_comparator(str(tmp_path)))
    assert issues and "exact" in issues[0], "exact_file 不依赖 shell 命令门，必须直接比较真实文件字节"


def test_criteria_standalone_legacy_cmp_is_rejected():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = '{"criteria": [{"id": "c1", "text": "精确内容", "type": "mechanical", "check": {"kind": "gate", "cmd": "printf \'A\' | cmp -s - target.txt"}}]}'

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    assert asyncio.run(mgr._synthesize_acceptance_criteria()) == []
    assert conv.deathmatch_acceptance_criteria is None, "standalone cmp 在命令门关闭时也是无效残余，必须整体拒绝"
    assert conv.deathmatch_status == "paused" and conv.deathmatch_reason, "无效合同不得 fail-open 继续计划"


def test_criteria_cmp_filename_gate_also_pauses_without_command_guessing():
    conv = _conv(deathmatch_goal="保留检查")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [
        {"id": "c1", "text": "target 至少 1B", "type": "mechanical",
         "check": {"kind": "file", "path": "target.txt", "min_bytes": 1}},
        {"id": "c2", "text": "cmp.txt 存在", "type": "mechanical",
         "check": {"kind": "gate", "cmd": "test -f cmp.txt"}},
    ]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    crit = asyncio.run(mgr._synthesize_acceptance_criteria())
    assert crit == [] and conv.deathmatch_status == "paused", "不再猜测命令位置：任何 gate 都不得进入新合同"


def test_criteria_rooted_backslash_exact_path_is_rejected():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [{
        "id": "c1", "text": "非法 rooted path", "type": "mechanical",
        "check": {"kind": "exact_file", "path": "\\etc\\passwd", "content": "A"},
    }]}, ensure_ascii=False)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    assert asyncio.run(mgr._synthesize_acceptance_criteria()) == []
    assert conv.deathmatch_acceptance_criteria is None, "必须先 normalize 再做 absolute 校验，不得留下不可满足合同"


def test_criteria_empty_or_reduced_repair_pauses_instead_of_failing_open():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    initial = json.dumps({"criteria": [
        {"id": "c1", "text": "target 至少 1B", "type": "mechanical",
         "check": {"kind": "file", "path": "target.txt", "min_bytes": 1}},
        {"id": "c2", "text": "精确 A", "type": "mechanical",
         "check": {"kind": "gate", "cmd": "printf 'A' | cmp -s - target.txt"}},
    ]}, ensure_ascii=False)
    replies = iter([initial, '{"criteria": []}'])

    async def fake_generate(*args, **kwargs):
        return next(replies)

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    assert asyncio.run(mgr._synthesize_acceptance_criteria()) == []
    assert conv.deathmatch_status == "paused" and conv.deathmatch_reason, "空/缩水重试不得清除机械验收后继续执行"


def test_exact_file_comparator_rejects_workspace_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("A", encoding="utf-8")
    (workspace / "target.txt").symlink_to(outside)
    conv = _conv(deathmatch_acceptance_criteria=[{
        "id": "exact", "text": "精确 A", "type": "mechanical", "source": "system",
        "check": {"kind": "exact_file", "path": "target.txt", "content": "A"},
    }])
    issues = asyncio.run(DeathmatchManager(conv)._run_goal_comparator(str(workspace)))
    assert issues and "工作区" in issues[0], "workspace symlink 指向外部文件不得满足 exact_file"


def test_plain_file_comparator_enforces_workspace_containment(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("A", encoding="utf-8")
    mgr = DeathmatchManager(_conv())
    assert mgr._check_file_condition(str(outside), 1, str(workspace)), "普通 file 不得接受 workspace 外绝对路径"
    (workspace / "target.txt").symlink_to(outside)
    assert mgr._check_file_condition("target.txt", 1, str(workspace)), "普通 file 不得接受 symlink 逃逸"
    inside = workspace / "inside.txt"
    inside.write_text("A", encoding="utf-8")
    assert mgr._check_file_condition(str(inside), 1, str(workspace)) == [], "workspace 内绝对路径仍应允许"


def test_criteria_surrogate_path_rejects_and_pauses():
    conv = _conv(deathmatch_goal="创建 target.txt")
    mgr = DeathmatchManager(conv)
    raw = json.dumps({"criteria": [{
        "id": "c1", "text": "非法 path", "type": "mechanical",
        "check": {"kind": "exact_file", "path": "\ud800", "content": "A"},
    }]}, ensure_ascii=True)

    async def fake_generate(*args, **kwargs):
        return raw

    mgr._make_llm = lambda **kwargs: object()
    mgr._llm_generate = fake_generate
    assert asyncio.run(mgr._synthesize_acceptance_criteria()) == []
    assert conv.deathmatch_status == "paused" and conv.deathmatch_reason, "非法 Unicode path 也必须走结构化重试/暂停而非通用异常清合同"


def _conv(**over):
    base = dict(
        id="conv-w1a",
        deathmatch_mode=True,
        deathmatch_status="active",
        deathmatch_goal="测试目标：产出一份报告",
        deathmatch_turns=0,
        deathmatch_max_turns=0,
        deathmatch_verify_failures=0,
        deathmatch_consecutive_failures=0,
        deathmatch_verdict=None,
        deathmatch_reason=None,
        deathmatch_plan={"steps": []},
        deathmatch_plan_version=1,
        deathmatch_reflections=[],
        deathmatch_last_verification_result=None,
        deathmatch_wall_time_started_at=None,
        deathmatch_max_wall_time_seconds=0,
        deathmatch_wall_time_used_seconds=0,
        deathmatch_grilling_qa_history=[],
        deathmatch_grilling_complete=True,
        deathmatch_human_gate=None,
        deathmatch_bible_draft=None,
        deathmatch_settled_ledger=None,
        deathmatch_subgoals=[],
        deathmatch_acceptance_criteria=None,
        deathmatch_failed_directions=None,
        deathmatch_pause_state=None,
        deathmatch_events=None,
        deathmatch_no_progress_replans=0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _prime_exec(goal, val=True):
    ds._EXECUTION_NEED_CACHE[ds._normalize_goal_key(goal)] = val


def _emp_mgr(**over):
    _prime_exec(_EMPIRICAL_GOAL, True)
    over.setdefault("deathmatch_goal", _EMPIRICAL_GOAL)
    over.setdefault("deathmatch_plan", {"steps": [_EMPIRICAL_STEP]})
    return DeathmatchManager(_conv(**over))


def _has_obligation(criteria):
    return any(
        isinstance(c, dict) and c.get("check", {}).get("kind") == "execution_content"
        for c in (criteria or [])
    )


# ── T1: freeze integrity (I8) ─────────────────────────────────────────────

def test_freeze_hash_includes_evidence_spec():
    a = {"id": "s1", "description": "d", "expected_output": "results.json",
         "verification_method": "", "boundary": "", "dependencies": [],
         "writes": [], "kind": "write", "tools": [],
         "done_check": {"mode": "none"}, "parallel_safe": False, "delegable": False}
    b = dict(a)
    b["evidence_spec"] = {
        "kind": "compute", "must_run": True,
        "receipt": {"cmds": ["python analysis/run.py"], "min_exit_code": 0,
                    "outputs": ["analysis/results.json"], "min_rows": 1},
        "on_failure": "blocked",
    }
    assert ds.step_statement_hash(a) != ds.step_statement_hash(b)


# ── T2/T3: evidence_spec normalization + protocol validation ─────────────

def test_plan_parse_normalizes_evidence_spec():
    mgr = DeathmatchManager(_conv())
    raw = json.dumps({"steps": [{
        "id": "s1", "description": "跑回归", "expected_output": "含系数表",
        "evidence_spec": {
            "kind": "compute", "must_run": True,
            "receipt": {"cmds": ["python a.py"], "outputs": ["r.json"]},
            "on_failure": "blocked",
        },
    }]})
    plan = mgr._parse_plan(raw)
    spec = plan["steps"][0]["evidence_spec"]
    assert spec["kind"] == "compute"
    assert spec["must_run"] is True
    assert spec["receipt"]["cmds"] == ["python a.py"]
    assert spec["receipt"]["outputs"] == ["r.json"]
    assert spec["on_failure"] == "blocked"


def test_validate_rejects_bad_evidence_spec():
    plan = {"steps": [{
        "id": "s1", "description": "d", "expected_output": "x",
        "verification_method": "",
        "evidence_spec": {"kind": "compute", "must_run": True,
                          "receipt": {"cmds": [], "outputs": []}},
    }]}
    issues = ds._validate_plan_protocol(plan)
    assert any("evidence_spec" in i for i in issues), issues


def test_numeric_step_requires_evidence_spec():
    """M3（r8）：由 agentic 步骤判定给出 require 集合 → 缺 evidence_spec 拒绝。"""
    plan = {"steps": [{
        "id": "s1", "description": "实证分析", "verification_method": "",
        "expected_output": "文件 empirical.md，含回归表系数与标准误",
    }]}
    issues = ds._validate_plan_protocol(plan, None, require_evidence_spec_ids={"s1"})
    assert any("缺少 evidence_spec" in i for i in issues), issues


def test_m3_scoped_to_new_step_ids():
    """r6（#4b）：M3 可限定作用域——旧数值步骤不因历史缺规格而阻塞 patch。"""
    plan = {"steps": [{
        "id": "legacy", "description": "旧实证步骤", "verification_method": "",
        "expected_output": "含系数表",
    }]}
    issues = ds._validate_plan_protocol(plan, None, require_evidence_spec_ids={"s9"})
    assert not any("缺少 evidence_spec" in i for i in issues), issues


# ── T4: obligation builder (mechanism only; trigger is agentic) ───────────

def test_builder_emits_execution_content_with_obligation_tag_and_live_paths():
    crit = ds._build_obligation_criteria([_EMPIRICAL_STEP], {"s5"})
    mk = [c for c in crit if c["check"]["kind"] == "execution_content"][0]
    assert mk.get("obligation") is True
    assert mk["check"]["paths"] == []          # 活计划解析（r6 #1/#3）
    assert mk["check"]["allow"] is False       # 恒定（原则：无关键词授权）
    assert all(c.get("obligation") is True for c in crit)
    # 被判定为数值结果步骤 → 产出文件存在性（含 evidence_spec.receipt.outputs 并集）
    assert any(c["check"]["kind"] == "file" and c["check"]["path"] == "empirical.md"
               for c in crit)


def test_builder_skips_file_checks_without_judged_ids():
    """r8：未判定为“需要数值结果”的步骤不生成文件存在性判据（无关键词触发）。"""
    crit = ds._build_obligation_criteria([_EMPIRICAL_STEP], set())
    assert not any(c["check"]["kind"] == "file" for c in crit)
    assert any(c["check"]["kind"] == "execution_content" for c in crit)


def test_builder_file_checks_from_evidence_spec_outputs_only():
    step = {
        "id": "s5", "description": "回归", "expected_output": "含系数与标准误的结果表",
        "evidence_spec": {"kind": "compute", "must_run": True,
                          "receipt": {"cmds": ["python run.py"],
                                      "outputs": ["analysis/results.json"]},
                          "on_failure": "blocked"},
    }
    crit = ds._build_obligation_criteria([step], {"s5"})
    assert any(c["check"]["kind"] == "file"
               and c["check"]["path"] == "analysis/results.json" for c in crit)


def test_builder_skips_unsafe_paths():
    step = dict(_EMPIRICAL_STEP, output_files=["../etc/passwd", "/abs/x", "ok.md"])
    crit = ds._build_obligation_criteria([step], {"s5"})
    paths = [c["check"]["path"] for c in crit if c["check"]["kind"] == "file"]
    assert paths == ["ok.md"]


async def _fake_step_ids_judge(system_prompt, user_prompt, **kwargs):
    return {"step_ids": ["s5"]}


def test_evidence_step_judgment_agentic_and_cached(monkeypatch):
    """r8（#5）：需要 evidence_spec 的步骤由 agentic 判定给出（非正则）。"""
    import app.services.agentic_judge as aj
    ds._EVIDENCE_STEP_IDS_CACHE.clear()
    calls = {"n": 0}

    async def fake(system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        return {"step_ids": ["s5"]}

    monkeypatch.setattr(aj, "judge_json", fake)
    mgr = _emp_mgr()
    ids = asyncio.run(mgr._judge_steps_needing_evidence_spec([_EMPIRICAL_STEP]))
    assert ids == {"s5"}
    ids2 = asyncio.run(mgr._judge_steps_needing_evidence_spec([_EMPIRICAL_STEP]))
    assert ids2 == {"s5"} and calls["n"] == 1, "判定结果必须缓存"


def test_evidence_step_judgment_failure_records_event_no_cache(monkeypatch):
    import app.services.agentic_judge as aj
    ds._EVIDENCE_STEP_IDS_CACHE.clear()
    calls = {"n": 0}

    async def none_judge(system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(aj, "judge_json", none_judge)
    conv = _conv(deathmatch_goal=_EMPIRICAL_GOAL)
    mgr = DeathmatchManager(conv)
    assert asyncio.run(
        mgr._judge_steps_needing_evidence_spec([_EMPIRICAL_STEP])
    ) is None
    assert any(
        e.get("type") == "evidence_step_judge_failed"
        for e in (conv.deathmatch_events or [])
    )
    asyncio.run(mgr._judge_steps_needing_evidence_spec([_EMPIRICAL_STEP]))
    assert calls["n"] == 2, "失败结果不得缓存（否则瞬时故障永久解武装）"


# ── T5: agentic trigger（execution need cache） ───────────────────────────

def test_execution_need_cache_gates_obligations():
    mgr = _emp_mgr()
    assert _has_obligation(mgr._obligation_criteria())
    _prime_exec(_EMPIRICAL_GOAL, False)
    assert mgr._obligation_criteria() == []


def test_unjudged_goal_not_obligated():
    """判定缓存未命中 → 保守不启用（fail-open，不误伤）。"""
    goal = "另一个尚未判定的目标"
    mgr = DeathmatchManager(_conv(
        deathmatch_goal=goal,
        deathmatch_plan={"steps": [{"id": "s1", "description": "d",
                                    "expected_output": "含系数表"}]},
    ))
    assert mgr._obligation_criteria() == []


def test_creative_goal_skips_obligations():
    goal = "写一部科幻小说：统计学家分析数据，样本来自面板观测，共 40 章"
    _prime_exec(goal, True)
    ds._CREATIVE_GOAL_CACHE[ds._normalize_goal_key(goal)] = True
    mgr = DeathmatchManager(_conv(
        deathmatch_goal=goal,
        deathmatch_plan={"steps": [{"id": "s1", "description": "第一章",
                                    "expected_output": "3000 字"}]},
    ))
    assert mgr._obligation_criteria() == []


# ── T6: persistence / epoch (M5 / I4 / r6 #1) ────────────────────────────

def test_apply_obligations_initial_persists_and_is_idempotent():
    mgr = _emp_mgr()
    assert mgr._apply_obligations(initial=True) >= 1
    stored = mgr._conv.deathmatch_acceptance_criteria or []
    assert _has_obligation(stored)
    assert mgr._apply_obligations(initial=True) == 0


def test_apply_obligations_epoch_guard_blocks_retro_gating():
    """I4：在途（无既有义务）会话不经 patch/replan 追溯启用。"""
    mgr = _emp_mgr()
    assert mgr._apply_obligations() == 0
    assert not _has_obligation(mgr._conv.deathmatch_acceptance_criteria)


def test_apply_obligations_appends_when_already_armed():
    mgr = _emp_mgr()
    assert mgr._apply_obligations(initial=True) >= 1
    # 后续计划新增数值步骤 → 追加文件存在性判据（append-only）
    conv = mgr._conv
    new_steps = [_EMPIRICAL_STEP, {
        "id": "s9", "description": "额外回归", "expected_output": "含显著性表",
        "output_files": ["extra.md"],
    }]
    conv.deathmatch_plan = {"steps": new_steps}
    # r8：文件判据由 agentic 步骤判定给出 → 先预热缓存（等价于 replan 前置判定）
    ds._EVIDENCE_STEP_IDS_CACHE[ds._evidence_step_ids_bucket(conv.deathmatch_goal, new_steps)] = ["s9"]
    assert mgr._apply_obligations() >= 1
    paths = [c["check"]["path"] for c in conv.deathmatch_acceptance_criteria
             if c.get("check", {}).get("kind") == "file"]
    assert "extra.md" in paths


def test_obligations_persisted_survive_plan_change():
    """M5：义务持久化后，计划去掉数值步骤也不消失。"""
    mgr = _emp_mgr()
    assert mgr._apply_obligations(initial=True) >= 1
    mgr._conv.deathmatch_plan = {"steps": [
        {"id": "s1", "description": "写摘要", "expected_output": "abstract.md 文字",
         "output_files": ["abstract.md"]},
    ]}
    assert _has_obligation(mgr._all_criteria())


def test_in_flight_conversation_not_retro_gated():
    mgr = _emp_mgr()
    assert not _has_obligation(mgr._all_criteria())


def test_kill_switch_disables_apply(monkeypatch):
    monkeypatch.setattr(DeathmatchManager, "_obligation_criteria_enabled",
                        lambda self: False)
    mgr = _emp_mgr()
    assert mgr._apply_obligations(initial=True) == 0
    assert not (mgr._conv.deathmatch_acceptance_criteria or [])


def test_kill_switch_hot_disables_persisted_obligations(monkeypatch):
    """r6（#2）：kill switch 关闭后，已持久化义务立即失效（热关）。"""
    mgr = _emp_mgr()
    assert mgr._apply_obligations(initial=True) >= 1
    assert _has_obligation(mgr._all_criteria())
    monkeypatch.setattr(DeathmatchManager, "_obligation_criteria_enabled",
                        lambda self: False)
    assert not _has_obligation(mgr._all_criteria())


# ── T7: agentic 占位判定（r7：无关键词表） ────────────────────────────────

def test_judge_uses_full_excerpt_and_blocks(tmp_path, monkeypatch):
    """判定输入必须包含表格中段正文（有界全文摘录），而非首尾片段。"""
    (tmp_path / "empirical.md").write_text(_PLACEHOLDER_MD, encoding="utf-8")
    mgr = DeathmatchManager(_conv())
    captured = {}
    import app.services.agentic_judge as aj

    async def fake_judge_json(system_prompt, user_prompt, **kwargs):
        captured["user"] = user_prompt
        return {"has_unexecuted_placeholders": True,
                "evidence": ["表6-2 受限估计：不填报数值"]}

    monkeypatch.setattr(aj, "judge_json", fake_judge_json)
    ds._PLACEHOLDER_JUDGE_CACHE.clear()
    out = asyncio.run(
        mgr._judge_unexecuted_placeholders(str(tmp_path), ["empirical.md"])
    )
    assert out["has_unexecuted_placeholders"] is True
    assert "受限估计：不填报数值" in captured["user"]


def test_judge_clean_deliverable_passes(tmp_path, monkeypatch):
    (tmp_path / "empirical.md").write_text(_CLEAN_MD, encoding="utf-8")
    mgr = DeathmatchManager(_conv())
    import app.services.agentic_judge as aj

    async def fake_judge_json(system_prompt, user_prompt, **kwargs):
        return {"has_unexecuted_placeholders": False}

    monkeypatch.setattr(aj, "judge_json", fake_judge_json)
    ds._PLACEHOLDER_JUDGE_CACHE.clear()
    out = asyncio.run(
        mgr._judge_unexecuted_placeholders(str(tmp_path), ["empirical.md"])
    )
    assert out["has_unexecuted_placeholders"] is False


def test_judge_fail_open_records_event(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("content", encoding="utf-8")
    conv = _conv()
    mgr = DeathmatchManager(conv)
    import app.services.agentic_judge as aj

    async def boom(system_prompt, user_prompt, **kwargs):
        raise RuntimeError("judge down")

    monkeypatch.setattr(aj, "judge_json", boom)
    ds._PLACEHOLDER_JUDGE_CACHE.clear()
    out = asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["a.md"]))
    assert out["has_unexecuted_placeholders"] is False
    assert any(
        e.get("type") == "placeholder_judge_failed"
        for e in (conv.deathmatch_events or [])
    )


def test_judge_live_paths_when_criterion_paths_empty(tmp_path, monkeypatch):
    """r6（#1/#3）：判据 paths 为空时按活计划产出解析（完成回填后仍武装）。"""
    (tmp_path / "paper.md").write_text(_PLACEHOLDER_MD, encoding="utf-8")
    conv = _conv(deathmatch_goal=_EMPIRICAL_GOAL,
                 deathmatch_plan={"steps": [_EMPIRICAL_STEP]})
    mgr = DeathmatchManager(conv)
    conv.deathmatch_plan["steps"][0]["output_files"] = ["paper.md"]
    captured = {}
    import app.services.agentic_judge as aj

    async def fake_judge_json(system_prompt, user_prompt, **kwargs):
        captured["user"] = user_prompt
        return {"has_unexecuted_placeholders": True}

    monkeypatch.setattr(aj, "judge_json", fake_judge_json)
    ds._PLACEHOLDER_JUDGE_CACHE.clear()
    out = asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), []))
    assert out["has_unexecuted_placeholders"] is True
    assert "paper.md" in captured["user"]


def test_no_keyword_tables_in_module():
    """用户红线（r7/r8）：占位/意图判定必须 agentic——禁止关键词表/正则回归。"""
    for banned in ("_PLACEHOLDER_MARKERS", "_PLACEHOLDER_BLOCKING_MARKERS",
                   "_PLACEHOLDER_MARKER_EXCLUSIONS", "_PLACEHOLDER_EXCLUSION_GUARDS",
                   "_DEGRADE_ALLOW_HINTS", "_EMPIRICAL_HINTS", "_NUMERIC_OUTPUT_RE"):
        assert not hasattr(ds, banned), banned
    assert not hasattr(DeathmatchManager, "_scan_unexecuted_placeholders")


def test_placeholder_judge_none_records_event_and_no_cache(tmp_path, monkeypatch):
    """r8（Critical）：judge_json 失败返回 None（不抛异常）→ 必须记事件且不缓存。"""
    (tmp_path / "a.md").write_text("content", encoding="utf-8")
    conv = _conv()
    mgr = DeathmatchManager(conv)
    import app.services.agentic_judge as aj
    calls = {"n": 0}

    async def none_judge(system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        return None

    monkeypatch.setattr(aj, "judge_json", none_judge)
    ds._PLACEHOLDER_JUDGE_CACHE.clear()
    out = asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["a.md"]))
    assert out.get("judge_failed") is True
    assert any(
        e.get("type") == "placeholder_judge_failed"
        for e in (conv.deathmatch_events or [])
    )
    asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["a.md"]))
    assert calls["n"] == 2, "未判定结果不得缓存"


def test_excerpt_per_file_truncation_marker(tmp_path):
    """r8（Important #2）：单文件超限必须显式标注截断（旧实现恒不披露）。"""
    (tmp_path / "big.md").write_text("x" * 70000 + "\n受限估计：待执行\n", encoding="utf-8")
    out = ds._read_deliverable_excerpt(str(tmp_path), ["big.md"])
    assert "文件截断" in out, "逐文件截断必须显式披露"


def test_excerpt_rejects_absolute_and_parent_paths(tmp_path):
    """r8（Security #3）：摘录只允许工作区内相对路径。"""
    outside = tmp_path.parent / "outside.md"
    outside.write_text("SECRET", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.md").write_text("ok", encoding="utf-8")
    out = ds._read_deliverable_excerpt(
        str(ws), [str(outside), "../outside.md", "ok.md"]
    )
    assert "SECRET" not in out
    assert "ok" in out


def test_legacy_markers_criterion_counts_as_armed_and_hot_filter(monkeypatch):
    """r8（Minor #6）：r5 旧契约（kind=markers、无 obligation tag）视为已武装，
    且 kill switch 关闭时同样被过滤。"""
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [_EMPIRICAL_STEP]},
        deathmatch_acceptance_criteria=[{
            "id": "ob1", "text": "旧占位判据",
            "type": "mechanical", "source": "system",
            "check": {"kind": "markers", "paths": [], "allow": False},
        }],
    )
    mgr = DeathmatchManager(conv)
    assert mgr._apply_obligations() >= 1, "旧 markers 契约必须视为已武装"
    monkeypatch.setattr(DeathmatchManager, "_obligation_criteria_enabled",
                        lambda self: False)
    assert not any(
        (c.get("check") or {}).get("kind") in ("markers", "execution_content")
        for c in mgr._all_criteria()
    )


def test_comparator_blocks_when_no_deliverable_files(tmp_path):
    """r8（Minor #8）：已武装契约但定位不到交付物 → 不得空转通过。"""
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [{
            "id": "s1", "description": "调研", "expected_output": "报告",
        }]},
    )
    mgr = DeathmatchManager(conv)
    assert mgr._apply_obligations(initial=True) >= 1
    issues = asyncio.run(mgr._run_goal_comparator(str(tmp_path)))
    assert any("无法定位交付物" in i for i in issues), issues


def test_comparator_blocks_when_declared_deliverable_missing(tmp_path):
    """r9（A4.9 Gap B）：声明了交付物但文件缺失/不可读 → 保守阻断（静默通过=漏洞）。"""
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [dict(_EMPIRICAL_STEP, output_files=["report.md"])]},
    )
    mgr = DeathmatchManager(conv)
    assert mgr._apply_obligations(initial=True) >= 1
    issues = asyncio.run(mgr._run_goal_comparator(str(tmp_path)))
    assert any("判定不可用" in i for i in issues), issues


def test_generate_goal_plan_fail_closed_on_trigger_judge_outage(monkeypatch):
    """r9（A4.9 Gap A）：触发判定不可用 → 不写计划（version 保持 0，可重试武装）。"""
    import app.services.agentic_judge as aj
    ds._EXECUTION_NEED_CACHE.clear()

    async def none_judge(system_prompt, user_prompt, **kwargs):
        return None

    monkeypatch.setattr(aj, "judge_json", none_judge)
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan=None,
        deathmatch_plan_version=0,
    )
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [{
            "id": "s1", "description": "调研", "expected_output": "report.md 报告",
            "dependencies": [],
        }]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    plan = asyncio.run(mgr.generate_goal_plan(db=None))
    assert plan is None, "触发判定不可用时必须 fail-closed"
    assert (conv.deathmatch_plan_version or 0) == 0, "version 必须保持 0 以便重试武装"
    assert any(
        e.get("type") == "execution_need_judge_failed"
        for e in (conv.deathmatch_events or [])
    )


def _patch_judge(mgr, verdict=True, evidence=("受限估计：待执行",)):
    async def _fake(workspace_path, files, obligation_context=""):
        return {"has_unexecuted_placeholders": bool(verdict), "evidence": list(evidence)}
    mgr._judge_unexecuted_placeholders = _fake


def test_placeholder_judge_context_changes_invalidate_cache(tmp_path, monkeypatch):
    """残留波 M1/M5：judge 缓存键 = 上下文+摘录——上下文变化必须重判。"""
    from app.services import deathmatch_service as _dm

    calls = {"n": 0}

    async def _fake_judge(prompt, content, **kw):
        calls["n"] += 1
        assert "任务要求上下文" in content
        return {"has_unexecuted_placeholders": False, "evidence": []}

    monkeypatch.setattr("app.services.agentic_judge.judge_json", _fake_judge)
    _dm._PLACEHOLDER_JUDGE_CACHE.clear()
    try:
        mgr = DeathmatchManager(_conv(deathmatch_goal="G1", deathmatch_plan={"steps": []}))
        (tmp_path / "paper.md").write_text("正文内容", encoding="utf-8")
        asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["paper.md"], "CTX-A"))
        asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["paper.md"], "CTX-A"))
        assert calls["n"] == 1, "相同上下文应命中缓存"
        asyncio.run(mgr._judge_unexecuted_placeholders(str(tmp_path), ["paper.md"], "CTX-B"))
        assert calls["n"] == 2, "上下文变化必须重判（不命中旧缓存）"
    finally:
        _dm._PLACEHOLDER_JUDGE_CACHE.clear()


def test_declared_output_files_final_first(tmp_path):
    mid = {"id": "s1", "description": "中间", "expected_output": "mid.md",
           "output_files": ["mid/one.md"]}
    final = {"id": "s8", "description": "合成", "expected_output": "paper.md 论文",
             "evidence_spec": {"kind": "compute", "receipt": {
                 "cmds": ["python build.py"], "outputs": ["paper.md"]}}}
    mgr = DeathmatchManager(_conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [mid, final]},
    ))
    files = mgr._declared_output_files()
    assert files[0] == "paper.md"
    assert "mid/one.md" in files


# ── T8: end-to-end gate (adversarial) ────────────────────────────────────

def test_gate_arms_after_completion_backfill(tmp_path):
    """r6（#1 Critical）：计划写入时无产出文件、完成回填后才出现 → 仍须阻断。"""
    _prime_exec(_EMPIRICAL_GOAL, True)
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [{
            "id": "s1", "description": "调研统计", "expected_output": "report.md 报告",
        }]},
    )
    mgr = DeathmatchManager(conv)
    assert mgr._apply_obligations(initial=True) >= 1  # 无文件也持久化 execution_content
    conv.deathmatch_plan["steps"][0]["output_files"] = ["report.md"]
    (tmp_path / "report.md").write_text(_PLACEHOLDER_MD, encoding="utf-8")
    _patch_judge(mgr, verdict=True)
    issues = asyncio.run(mgr._run_goal_comparator(str(tmp_path)))
    assert any("未执行占位" in i for i in issues), issues


def test_a104_like_case_blocks_done(tmp_path):
    """对抗认证：与 a104fbc5 同型的占位论文必须在完成闸门被拦。"""
    _prime_exec(_EMPIRICAL_GOAL, True)
    (tmp_path / "paper.md").write_text(
        "# 论文\n\n## 6 实证分析\n" + _PLACEHOLDER_MD, encoding="utf-8"
    )
    conv = _conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [
            dict(_EMPIRICAL_STEP, id="s8", description="合成论文",
                 expected_output="文件 paper.md（1.5–2 万字，含实证结果）",
                 output_files=["paper.md"]),
        ]},
    )
    mgr = DeathmatchManager(conv)
    assert mgr._apply_obligations(initial=True) >= 1
    _patch_judge(mgr, verdict=True)
    issues = asyncio.run(mgr._run_goal_comparator(str(tmp_path)))
    assert issues, "占位论文不得通过机械验收"
    assert any("未执行占位" in i for i in issues), issues


# ── T9: local_patch ───────────────────────────────────────────────────────

def test_local_patch_preserves_evidence_spec():
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "s1", "status": "in_progress", "description": "a", "expected_output": "b",
         "dependencies": [], "writes": [], "attempts": 0, "recovery": "idle",
         "evidence_spec": {"kind": "compute", "must_run": True,
                           "receipt": {"cmds": ["python a.py"], "outputs": ["r.json"]},
                           "on_failure": "blocked"}},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [{"id": "s1", "status": "pending",
                                      "description": "更细的步骤", "expected_output": "b",
                                      "dependencies": []}]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    ok = asyncio.run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["缺文件"]))
    assert ok is True
    es = conv.deathmatch_plan["steps"][0].get("evidence_spec")
    assert es and es["receipt"]["outputs"] == ["r.json"]


def test_local_patch_allows_legacy_numeric_step_without_spec(monkeypatch):
    """r6（#4b）：旧数值步骤缺 evidence_spec 不阻塞 patch（M3 仅限被 patch 步骤）。

    fix-round 1（评审 A-2 hermetic 钉）：原测不夹 judge_json——live judge 可达时
    「含系数表」命中数值触发词会被 require_evidence_spec_ids 拒（该测只在 judge
    失败时绿=非密封 flaky，CI-parity 随环境翻红）。现钉 judge 不可达降级路径
    （none_judge=本测断言的原始语境）；「活判官拒旧数值步」的语义分歧挂账
    memory ⏳ 待专项，不在本波裁定（ADR D-r6）。
    """
    import app.services.agentic_judge as aj

    async def none_judge(system_prompt, user_prompt, **kwargs):
        return None

    monkeypatch.setattr(aj, "judge_json", none_judge)
    conv = _conv(deathmatch_plan={"steps": [
        {"id": "legacy", "status": "in_progress", "description": "旧实证",
         "expected_output": "含系数表", "dependencies": [], "writes": []},
    ]})
    mgr = DeathmatchManager(conv)

    async def fake_gen(llm, system_prompt, user_prompt, **kwargs):
        return json.dumps({"steps": [{"id": "legacy", "status": "pending",
                                      "description": "更细", "expected_output": "b",
                                      "dependencies": []}]}, ensure_ascii=False)

    mgr._make_llm = lambda **kw: object()
    mgr._llm_generate = fake_gen
    ok = asyncio.run(mgr._local_patch_step(conv.deathmatch_plan["steps"][0], ["缺文件"]))
    assert ok is True


# ── T10: config-level kill switch ────────────────────────────────────────

def test_config_level_kill_switch(monkeypatch):
    cfg = ds.config
    monkeypatch.setattr(
        type(cfg), "deathmatch_obligation_criteria_enabled",
        property(lambda self: False),
    )
    _prime_exec(_EMPIRICAL_GOAL, True)
    mgr = DeathmatchManager(_conv(
        deathmatch_goal=_EMPIRICAL_GOAL,
        deathmatch_plan={"steps": [_EMPIRICAL_STEP]},
    ))
    assert mgr._obligation_criteria_enabled() is False
    assert mgr._obligation_criteria() == []
