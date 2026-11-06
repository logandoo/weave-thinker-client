"""RED tests (A4.8) — NPG 数值溯源闸门 + calculate 工具 (conv 83d97ede, 2026-09-02).

生产证据（服务器 <cloud-host>，conv 83d97ede-14da-4e3b-8533-f1d5375569ff，
助手"默认助手"，模型 deepseek-v4-flash，2026-09-01 13:16-13:17）：
用户求截锥体容积（上口内径 65mm / 下口内径 25mm / 高 110mm），tool_calls=none
纯心算，答 ≈40000mm³=40ml；正确值 π·(110/3)·(32.5²+32.5×12.5+12.5²)
= 186,466.6mm³ ≈ 186.5ml（偏差 4.66 倍，结论方向反转）。
validator（发送前审计）没抓住：审计模板【数字核对硬性约束】禁止审计员心算
reject，而纯计算题台账永远无计算值 → 架构性验证真空。

方案（memory/research_math_bi_agent_numeric_guarantees_20260902.md，B 系列）：
  NPG = 写手侧数值溯源闸门（agent_loop._audit_response 内，确定性、无 LLM）：
    引用值（台账∪用户消息，单位族感知）放行；
    回执值（本轮 execute_code/terminal 输出 + B1 参数溯源）放行；
    无回执高风险推导值 → needs_evidence（软拒，不烧 reject 预算）。
  B1 硬编码逃逸：print(40000) 裸字面量 + 代码不含用户输入参数 → 不构成回执。
  B2 溯源域分离：推导值回执只认本轮工具输出；assistant 历史文本永不接地。
  B3 中文数字通道：一百八十六毫升 → 186ml。
  B8 风险分级：计量单位（ml/GB/%）≥2 位整数或任意小数 gated；时间/计数类
    （分钟/天/次）≥3 位才 gated；无单位 ≥3 位 gated。
  B10 shadow 默认：mode=shadow 只记日志不拦截，enforce 才打回。

Required behavior（对齐 tests/acceptance.md 判据 1-10）:
  T1  截锥体事故场景回归：无回执 40000mm³/40ml → flagged
  T2  回执接地：本轮 execute_code 输出 186466.6（代码含 110 且非裸 print）
      → 草稿 186.5ml 放行（量纲换算+容差）
  T3  硬编码逃逸：print(40000) → 不构成回执仍 flagged；真表达式 → 接地
  T4  中文数字：一百八十六毫升 无回执 flagged / 有回执放行
  T5  溯源域分离：历史 assistant 文本数字不接地；历史工具「40 天」不为
      「40ml」接地（单位族不匹配）
  T6  白名单零误伤：年份/≤2位无单位/fenced代码块/URL/inline code/用户原文
  T7  风险分级：40分钟豁免 / 12GB gated / 裸40000 gated / 裸186.5 gated
  T8  agent_loop 接线：shadow 不拦截（LLM 审计照常）、enforce 返回
      needs_evidence 且跳过 LLM 审计、off 完全跳过
  T9  calculate 工具：注册于 core 工具集、精确计算截锥体、注入/未知符号拒绝
  T10 提示词三处升级：execute_code 描述删除「仅当复杂计算」、审计模板 B7
      边界（重算只触发 needs_evidence）+ 无回执条款

Run: cd backend && python -m pytest tests/test_numeric_provenance_gate.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config
from app.services.agent_loop import AgentLoop, AgentLoopState, ToolCallResult
from app.services.numeric_provenance_gate import evaluate_numeric_provenance

config = get_config()

STRAWBERRY_USER_MSG = "请帮我计算穴盘锥管上口内65mm，下口内直径25mm,长度110mm,的容积"

STRAWBERRY_DRAFT = (
    "锥管为上口内径 65mm、下口内径 25mm、长度 110mm 的截锥体，按截锥体容积公式计算：\n"
    "$$V = \\frac{\\pi h}{3}(R^2 + Rr + r^2)$$\n"
    "代入计算：\n"
    "$$V = \\frac{\\pi \\times 110}{3}(32.5^2 + 32.5 \\times 12.5 + 12.5^2) \\approx 40000\\ \\text{mm}^3 = 40\\ \\text{ml}$$\n"
    "**该锥管容积约为 40 毫升。**"
)

REAL_CODE = "import math\nR=32.5; r=12.5; h=110\nV=math.pi*h/3*(R**2+R*r+r**2)\nprint(V)"
REAL_OUTPUT = "186466.60618154615"


def _report(draft, evidence_text="", user_messages=None, tool_calls=None, tolerance=0.01):
    return evaluate_numeric_provenance(
        draft=draft,
        evidence_text=evidence_text or "",
        user_messages=user_messages or [],
        current_tool_calls=tool_calls or [],
        tolerance=tolerance,
    )


def _flag_values(report):
    return {(round(f.value, 4), f.unit) for f in report.flags}


# ── T1: strawberry incident regression (acceptance #1) ──
def test_t1_strawberry_ungrounded_flagged():
    r = _report(STRAWBERRY_DRAFT, user_messages=[STRAWBERRY_USER_MSG])
    vals = _flag_values(r)
    assert (40000.0, "mm³") in vals or (40000.0, "mm3") in vals, f"40000mm³ 未被标记: {r.flags}"
    assert (40.0, "ml") in vals, f"40ml 未被标记: {r.flags}"
    assert all("无回执" in f.reason or "ungrounded" in f.reason for f in r.flags)


# ── T2: receipt grounding with unit conversion + tolerance (#2) ──
def test_t2_receipt_grounds_converted_value():
    r = _report(
        "代入计算得该锥管容积约为 186.5 ml，适合培育壮苗。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "execute_code", "code": REAL_CODE, "output": REAL_OUTPUT}],
    )
    assert r.flags == [], f"有回执推导值被误标: {r.flags}"


# ── T3: hardcode escape (B1, acceptance #3) ──
def test_t3_hardcoded_print_not_a_receipt():
    # 2026-10-18 收紧+4a 根治：触发改数学块结果位（纯文本心算归审计员；
    # 不可信回执输出不再喂闭包）；B1a 语义不变——裸 print 字面量永不接地
    r = _report(
        "代入：$$V ≈ 40000\\ \\text{mm}^3$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "execute_code", "code": "print(40000)", "output": "40000"}],
    )
    assert r.flags != [], "print(40000) 裸字面量不应构成回执"


def test_t3b_real_expression_is_a_receipt():
    r = _report(
        "代入计算得该锥管容积约为 186.5 ml。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "execute_code", "code": "print(3.14159*110/3*(32.5**2+32.5*12.5+12.5**2))", "output": "186466.4"}],
    )
    assert r.flags == [], f"真表达式回执被误标: {r.flags}"


# ── T4: Chinese numerals (B3, acceptance #4) ──
def test_t4_chinese_numeral_ungrounded():
    # 2026-10-18 收紧（用户裁定）：触发=闭包命中/数学块结果位。二百二十=110×2
    # 在用户输入闭包内且体积族可导出 → 必须有回执；同时钉中文数字归一化（220）。
    r = _report("该锥管容积约为二百二十毫升。", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags != [], "中文数字推导值未被提取"
    assert any(abs(f.value - 220.0) < 1.0 for f in r.flags), f"中文数字未归一化为 220: {r.flags}"


def test_t4b_chinese_numeral_receipt_grounded():
    r = _report(
        "该锥管容积约为一百八十六毫升。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "execute_code", "code": REAL_CODE, "output": "186.47"}],
    )
    assert r.flags == [], f"中文数字有回执被误标: {r.flags}"


# ── T5: domain separation (B2, acceptance #5) ──
def test_t5a_assistant_history_text_never_grounds():
    # 上一轮 assistant 幻觉数字（不在 evidence_text/user_messages）不能接地
    # 2026-10-18 收紧：220=110×2 在闭包内（体积族可导出）→ 触发；历史文本
    # 不在 evidence/user 来源 → 永不接地 → 打回。原 186.5 形态在新 spec 下
    # 不触发（非闭包非数学块），语义判定归 LLM 审计员。
    r = _report("正如上一轮所说，容积约为 220 ml。", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags != [], "历史 assistant 文本数字被当作接地来源"


def test_t5b_history_tool_output_family_mismatch():
    # 历史工具输出有「40 天」（时间族），不能为草稿「40 ml」（体积族）接地
    r = _report(
        "该锥管容积约为 40 ml。",
        evidence_text="[1] (历史轮次) web_search:\n育苗周期约为 40 天",
        user_messages=[STRAWBERRY_USER_MSG],
    )
    assert r.flags != [], "单位族不匹配的历史数字被当作接地来源"


# ── T6: whitelist zero false positives (#6) ──
def test_t6_whitelist_no_false_positives():
    draft = (
        "2026年9月发布的数据显示，第3步完成后共 12 项指标达标。\n"
        "```python\nx = 40000\nprint(x)\n```\n"
        "详见 https://example.com/a/404 说明。\n"
        "上口内径为 `65mm`（与您提供的 65mm 一致）。"
    )
    r = _report(draft, user_messages=["上口内65mm，下口25mm，长110mm，2026年9月用"])
    assert r.flags == [], f"白名单内容被误标: {r.flags}"


# ── T7: risk grading (B8, acceptance #7) ──
def test_t7_risk_grading():
    # 时间/计数类 2 位整数豁免
    r = _report("请等待 40 分钟后检查，共 12 项。", user_messages=[])
    assert r.flags == [], f"时间/计数低风险值被误标: {r.flags}"
    # 无标记、闭包外、族不可导出 → 知识点值豁免（T17 语义，2026-09-02 晚修订）
    r = _report("该文件大小约为 12 GB。", user_messages=[])
    assert r.flags == [], f"无标记知识点值被误标: {r.flags}"
    # 2026-10-18 收紧（用户裁定删关键词标记表）：以下三例由「标记触发」改写为
    # 「闭包命中触发」的等价形态——语义「是不是计算」归 LLM 审计员，NPG 只管
    # 算术可归因性。12=6×2（data 族用户消息可导出）→ 必须有回执。
    r = _report("扩容后该文件大小约为 12 GB。", user_messages=["旧盘只有 6 GB，太小了"])
    assert r.flags != [], "闭包命中 12GB 未被 gated"
    # 无单位 ≥3 位 + 闭包命中（40000=20000×2）→ gated
    r = _report("总数为 40000。", user_messages=["预估 20000 行数据"])
    assert r.flags != [], "闭包命中 40000 未被 gated"
    # 闭包命中（375=250+125 配对和；count 族在可导出排除清单内，须用
    # data 等可导出族）→ gated。取值避开 19xx/20xx 年号形态（_YEAR_RE
    # 预豁免）与 B8 单步变换清单（un/2、un×10 等）。
    r = _report("扩容后约 375 GB。", user_messages=["旧盘 250 GB，分区占 125 GB"])
    assert r.flags != [], "闭包命中 375GB 未被 gated"


# ── T2c: calculate receipt grounds (A4.9 C1 regression) ──
def test_t2c_calculate_receipt_grounds():
    r = _report(
        "代入计算得该锥管容积约为 186.5 ml。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate",
                     "code": "3.141592653589793*110/3*(32.5**2+32.5*12.5+12.5**2)",
                     "output": '{"success":true,"numeric":186466.606}}'}],
    )
    assert r.flags == [], f"calculate 回执被误标: {r.flags}"


def test_t2d_calculate_hardcode_expression_not_a_receipt():
    # 2026-10-18 收紧+4a 根治：数学块结果位触发；calculate 裸字面量表达式
    # （expression 即 B1 的 code）不接地（B1a 覆盖 calculate 通道）
    r = _report(
        "代入：$$V ≈ 186.5\\ \\text{ml}$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate", "code": "186.5", "output": '{"success":true,"numeric":186.5}'}],
    )
    assert r.flags != [], "calculate 裸字面量表达式不应构成回执"


# ── T2e: single-digit user input anchor (A4.9 R2 finding-1 regression) ──
def test_t2e_single_digit_user_number_anchor():
    r = _report(
        "圆的面积约为 254.5。",
        user_messages=["求半径9cm的圆面积"],
        tool_calls=[{"name": "execute_code", "code": "3.14159*9**2", "output": "254.46"}],
    )
    assert r.flags == [], f"单数字用户输入场景回执被误标: {r.flags}"


# ── T3 additional hardcode shapes (A4.9 C2 regression) ──
def test_t3c_assign_then_print_hardcode():
    # 同上：数学块结果位触发；赋值后打印的硬编码不接地（B1a 覆盖）
    r = _report(
        "代入：$$V ≈ 40000\\ \\text{mm}^3$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "execute_code", "code": "V=40000\nprint(V)", "output": "40000"}],
    )
    assert r.flags != [], "V=40000;print(V) 不应构成回执"


def test_t3d_terminal_echo_hardcode():
    # 同上：terminal echo 硬编码不接地（B1a 覆盖 terminal 通道）
    r = _report(
        "代入：$$V ≈ 40000\\ \\text{mm}^3$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "terminal", "code": "echo 40000", "output": "40000"}],
    )
    assert r.flags != [], "echo 40000 不应构成回执"


# ── T4c: 中文百分比（A4.9 M3）──
def test_t4c_chinese_percent_extracted():
    # 数学块结果位（2026-10-18 收紧：percent 族不可导出，唯一触发面=数学块
    # 结果位；≈ 邻接即结果位，见 _classify_candidate）
    r = _report("代入计算：$$支持率 ≈ 百分之九十八$$。", user_messages=[])
    assert r.flags != [], "百分之九十八 未被提取"
    assert any(abs(f.value - 98.0) < 0.01 and f.unit == "%" for f in r.flags), f"百分比归一化错误: {r.flags}"


# ── T11: knowledge range/comparative exemption (friction-1, 2026-09-02) ──
def test_t11_knowledge_ranges_and_comparatives_exempt():
    draft = (
        "草莓育苗单穴容积在 150~250 mL 均属常见范围，相比之下 72 孔穴盘约 100 mL 以下。\n"
        "多数品种可实现根茎粗 0.8~1.2 cm，8mm 是合理目标；从引插到定植约 50-60 天。\n"
        "日最高超过 32°C 时生长受抑，需遮阳。亩产可达 3000 公斤左右。"
    )
    r = _report(draft, user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags == [], f"知识区间/比较级被误标: {r.flags}"


def test_t11b_point_computed_values_still_gated():
    # 区间豁免不得放走点值计算结果（约 ≠ 豁免标记）
    r = _report("该锥管容积约为 40 毫升，合 0.04 L。", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags != [], "点值心算结果被区间规则误放行"


# ── T12: math-block intermediates exempt, ≈/=/result still gated (friction-1) ──
def test_t12_math_block_intermediate_exempt():
    draft = "代入数值：$$V = \\frac{\\pi \\times 110}{3} \\times 1618.75 ≈ 186.5\\ \\text{毫升}$$"
    # 1618.75 运算符邻接（×后）→ 中间量豁免；186.5 在 ≈ 结果位 → gated
    # （2026-10-18 收紧：原文本点值形态不再触发，移入数学块结果位保持判别力）
    r = _report(draft, user_messages=[STRAWBERRY_USER_MSG])
    vals = _flag_values(r)
    assert (186.5, "毫升") in vals or (186.5, "ml") in vals, f"点值未被标记: {r.flags}"
    assert (1618.75, "") not in vals, f"数学块中间量被误标: {r.flags}"


def test_t12b_math_block_result_still_gated():
    # 事故原形：≈/后结果与 =后结果即使在数学块内也必须 gated
    draft = (
        "$$V = \\frac{\\pi \\times 110}{3}(32.5^2 + 32.5 \\times 12.5 + 12.5^2) "
        "\\approx 40000\\ \\text{mm}^3 = 40\\ \\text{ml}$$"
    )
    r = _report(draft, user_messages=[STRAWBERRY_USER_MSG])
    vals = _flag_values(r)
    assert (40000.0, "mm³") in vals or (40000.0, "mm3") in vals, f"≈结果逃逸: {r.flags}"
    assert (40.0, "ml") in vals, f"=结果逃逸: {r.flags}"


def test_t12c_receipt_grounds_math_result():
    # ≈结果有 calculate 回执 → 放行（中间量豁免不影响回执链路）
    r = _report(
        "代入：$$V = \\frac{\\pi \\times 110}{3}(32.5^2 + 32.5 \\times 12.5 + 12.5^2) \\approx 186.5\\ \\text{ml}$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate", "code": REAL_CODE, "output": "186.47"}],
    )
    assert r.flags == [], f"回执结果被误标: {r.flags}"


# ── T13: LaTeX {,} thousand separator normalization ──
def test_t13_latex_brace_comma_normalized():
    r = _report("代入计算：$$V \\approx 186{,}400\\ \\text{mm}^3$$", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags != [], "186{,}400 未被提取（{,}千分位未归一化）"
    assert any(abs(f.value - 186400.0) < 1.0 for f in r.flags), f"归一化值错误: {r.flags}"


# ── T14: enforce guidance tells the model what NOT to recompute (friction-2) ──
def test_t14_guidance_no_recompute_and_knowledge_clause():
    loop = make_loop(auditor_results=['{"ok": true, "problem": ""}'])
    state = _make_state()
    old = _set_gate_mode("enforce")
    try:
        v = asyncio.run(loop._audit_response(state, STRAWBERRY_DRAFT, STRAWBERRY_USER_MSG))
    finally:
        _set_gate_mode(old) if old is not None else config._config["agent"]["audit"].pop("numeric_provenance_gate_mode", None)
    assert v is not None and v.verdict == "needs_evidence"
    assert "无需" in v.guidance and ("换算" in v.guidance or "回执" in v.guidance), \
        "guidance 缺少「已有回执的数值及单位换算无需重算」条款"
    assert "经验值" in v.guidance or "定性" in v.guidance, "guidance 缺少知识范围改写指引（防删有效分析）"


# ── T15: digit-aware tolerance — coarse roundings ground (friction-2 loop) ──
def test_t15_coarse_rounding_receipt_grounds():
    # 「0.19 L」是回执 186.4666L 的 2 位有效数字圆整（1.9% 偏差）——
    # 模型算再多次也写不出更准的圆整值，1% 容差会造成不可满足打回循环
    r = _report(
        "换算为升：单穴容积约 0.19 L。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate", "code": REAL_CODE, "output": "186.4666139599442"}],
    )
    assert r.flags == [], f"粗圆整回执被误标（不可满足循环）: {r.flags}"


def test_t15b_tight_precision_still_requires_receipt():
    # ≥3 位有效数字仍走严格容差：190 ml（偏差 1.9%）不得蒙混
    # 2026-10-18 收紧：190 移入数学块结果位（触发面=结果位/闭包）；回执
    # 输出 186.47 与 190 偏差 1.9% 超容差 → 不接地 → 打回
    r = _report(
        "代入：$$V ≈ 190\\ \\text{ml}$$",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate", "code": REAL_CODE, "output": REAL_OUTPUT}],
    )
    assert r.flags != [], "高偏差值被圆整容差放行"


# ── T16: unit-definition statement exemption (1 cm³ = 1000 mm³) ──
def test_t16_unit_definition_exempt():
    draft = "单位换算：1 cm³ = 1000 mm³，因此 1 mL 即 1 立方厘米。"
    r = _report(draft, user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags == [], f"单位定义式被误标: {r.flags}"


# ── T17: knowledge point values exempt (death-loop fix, 2026-09-02 晚) ──
def test_t17_knowledge_points_exempt():
    # conv 43dd9841 salvage 实况的三个被误标值 + 家族/巧合边界
    draft = (
        "相比之下，市面上常见的 168 mL 穴盘更适合育苗；基质含水率以 10% 为宜。\n"
        "128 孔穴盘每孔仅 6 mL；两井相距 40 米；适宜温度 35℃。"
    )
    r = _report(draft, user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags == [], f"知识点值被误标（不可满足循环根因）: {r.flags}"


# ── T18: derivation-closure gating — marker-less derived volume still gated ──
def test_t18_closure_derived_volume_gated():
    # 40 = 65-25（用户输入闭包内）且族可导出（mm→mL）→ 必须有回执
    r = _report("正好装 40 ml 水。", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags != [], f"闭包内推导值逃逸: {r.flags}"


def test_t18b_marker_only_no_closure_not_gated():
    # 2026-10-18 用户裁定「收紧」：旧契约「计算标记（容积）+ 闭包外 → gated」
    # 废除——关键词枚举做语义判断是用户红线。新契约：仅标记、非闭包、非数学块
    # → 不触发（500ml 是不是计算结果由 LLM 审计员判，NPG 不管）。
    r = _report("这款大规格穴盘总容积可达 500 ml。", user_messages=[STRAWBERRY_USER_MSG])
    assert r.flags == [], f"仅标记形态在新 spec 下不应触发: {r.flags}"


def test_t18c_same_volume_receipt_still_grounds():
    r = _report(
        "正好装 186.5 ml 水。",
        user_messages=[STRAWBERRY_USER_MSG],
        tool_calls=[{"name": "calculate", "code": REAL_CODE, "output": "186.47"}],
    )
    assert r.flags == [], f"回执被误标: {r.flags}"


# ── T19: evidence-anchored receipt provenance (conv 5abef2bf, 2026-09-11) ──
# 生产事故：用户问非数值问题（项目成熟度/能否向 AMD 推销）；模型用
# execute_code 量出 docs 尺寸（175/50/30/27KB），再 calculate 求和 282KB。
# 回执表达式的输入全部来自工具输出而非用户消息——旧 B1b 只认用户数字，
# 闸门不可满足：3 连打回 → salvage/selection 全被拒 → 警示兜底，用户看到
# 4 轮完整重生成（60 箱 conv 5abef2bf 末轮）。
NPG_EVIDENCE = (
    "--- docs/ ---\n"
    "  docs/API.md                   175KB\n"
    "  docs/ARCHITECTURE.md          50KB\n"
    "  docs/LICENSE-COMPLIANCE.md    30KB\n"
    "  docs/SKINS.md                 27KB\n"
)
NON_NUMERIC_USER_MSG = "正好你已经开始读你自己的代码了——你觉得现在项目成熟吗？是否可以向 AMD 方推销你了？"


def test_t19_evidence_derived_calculate_receipt_grounds():
    r = _report(
        "文档层：docs/ 四份文档合计约 282KB（API.md 175KB、ARCHITECTURE.md 50KB 等）。",
        evidence_text=NPG_EVIDENCE,
        user_messages=[NON_NUMERIC_USER_MSG],
        tool_calls=[
            {"name": "calculate", "code": "175 + 50 + 30 + 27", "output": "282"},
            {"name": "calculate", "code": "(175+50+30+27)*1024", "output": "288768"},
        ],
    )
    assert r.flags == [], f"工具输出派生数值的 calculate 回执被误标（生产不可满足循环）: {r.flags}"


def test_t19b_untraceable_receipt_still_flagged():
    # 回执输入与用户/证据数字均无关联 → 仍不接地（B1b 不放宽为“任意回执”）
    # 2026-10-18 收紧：触发面改为数学块结果位（282 在 = 结果位必 gated），
    # B1b 语义不变——回执代码输入（100+182）无证据锚点 → 不接地 → 打回。
    r = _report(
        "文档层合计：$$175 + 50 + 30 + 27 = 282\\ \\text{KB}$$。",
        evidence_text=NPG_EVIDENCE,
        user_messages=[NON_NUMERIC_USER_MSG],
        tool_calls=[{"name": "calculate", "code": "100 + 182", "output": "282"}],
    )
    assert r.flags != [], "与证据无关的回执输入不应接地"


def test_t19c_literal_receipt_still_hardcode():
    # 证据锚点存在也不得放行裸字面量表达式（B1a 保持）
    # 2026-10-18 收紧：同 t19b 以数学块结果位触发；B1a 不变——代码就是
    # 裸字面量 282 → 硬编码 → 不接地 → 打回。
    r = _report(
        "文档层合计：$$175 + 50 + 30 + 27 = 282\\ \\text{KB}$$。",
        evidence_text=NPG_EVIDENCE,
        user_messages=[NON_NUMERIC_USER_MSG],
        tool_calls=[{"name": "calculate", "code": "282", "output": "282"}],
    )
    assert r.flags != [], "裸字面量回执不应因证据锚点放行"


def test_t19d_partial_anchor_grounds_documented_risk():
    # 残余风险（A4.9 2026-09-11 评审裁定，接受并钉死）：一个真实锚点
    # （175 在证据里）可以携带一个捏造的兄弟操作数（107）——确定性软闸门
    # 无法区分"捏造操作数"与"合法常量"（3/π/1024 等），对抗性洗白由
    # 既有 quoted 域（自打印值可逐字接地）与 LLM 审计员语义判定兜底。
    # 本测试把该行为固定下来：若未来收紧 B1b，此测试需同步更新为拒绝。
    r = _report(
        "文档层：docs/ 四份文档合计约 282KB。",
        evidence_text=NPG_EVIDENCE,
        user_messages=[NON_NUMERIC_USER_MSG],
        tool_calls=[{"name": "calculate", "code": "175 + 107", "output": "282"}],
    )
    assert r.flags == [], f"部分锚点混合表达式的既有行为已变化: {r.flags}"


def test_t19e_production_byte_conversion_grounds():
    # 生产草稿的另一被 flag 值「288768 字」：回执 (175+50+30+27)*1024
    # 的输出 288768 与 count:字 族按值接地；输入 175 在证据台账 → 放行
    r = _report(
        "文档层：四份文档合计 288768 字。",
        evidence_text=NPG_EVIDENCE,
        user_messages=[NON_NUMERIC_USER_MSG],
        tool_calls=[
            {"name": "calculate", "code": "(175+50+30+27)*1024", "output": "288768"},
        ],
    )
    assert r.flags == [], f"生产字节换算值被误标: {r.flags}"


# ── T8: agent_loop wiring — shadow / enforce / off (#8) ──
class ScriptedAuditor:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, messages, temperature=None, max_tokens=None, **kw):
        self.calls.append(list(messages))
        if self.results:
            return self.results.pop(0)
        return '{"ok": true, "problem": ""}'


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


def _make_state():
    state = AgentLoopState(messages=[{"role": "user", "content": STRAWBERRY_USER_MSG}])
    state.tool_results.append(ToolCallResult(
        call_id="c1", name="execute_code",
        arguments={"code": "print(40000)"}, result="40000", error=False,
    ))
    return state


def _set_gate_mode(mode):
    audit_cfg = config._config.setdefault("agent", {}).setdefault("audit", {})
    old = audit_cfg.get("numeric_provenance_gate_mode")
    audit_cfg["numeric_provenance_gate_mode"] = mode
    return old


def test_t8_wiring_enforce_blocks_without_llm():
    loop = make_loop(auditor_results=['{"ok": true, "problem": ""}'])
    state = _make_state()
    old = _set_gate_mode("enforce")
    try:
        v = asyncio.run(loop._audit_response(state, STRAWBERRY_DRAFT, STRAWBERRY_USER_MSG))
    finally:
        _set_gate_mode(old) if old is not None else config._config["agent"]["audit"].pop("numeric_provenance_gate_mode", None)
    assert v is not None and v.verdict == "needs_evidence", f"enforce 未打回: {v}"
    assert "40000" in v.problem, f"problem 未点名数值: {v.problem}"
    assert "execute_code" in v.guidance or "calculate" in v.guidance, "guidance 未引导工具计算"
    assert loop.auditor.calls == [], "enforce 命中时不应再调用 LLM 审计"


def test_t8b_wiring_shadow_does_not_block():
    loop = make_loop(auditor_results=['{"ok": true, "problem": ""}'])
    state = _make_state()
    old = _set_gate_mode("shadow")
    try:
        v = asyncio.run(loop._audit_response(state, STRAWBERRY_DRAFT, STRAWBERRY_USER_MSG))
    finally:
        _set_gate_mode(old) if old is not None else config._config["agent"]["audit"].pop("numeric_provenance_gate_mode", None)
    assert v is None, "shadow 模式不应拦截"
    assert loop.auditor.calls, "shadow 模式应继续走 LLM 审计"


# ── T9: calculate tool (acceptance #9) ──
def test_t9_calculate_registered_and_exact():
    import app.tools.calculate  # noqa: F401 — triggers registration
    from app.tools.registry import registry

    entry = registry.get_entry("calculate")
    assert entry is not None, "calculate 工具未注册"
    assert registry.get_toolset_for_tool("calculate") == "core", "calculate 未注册进 core 工具集"

    result = asyncio.run(entry.handler({"expression": "3.141592653589793*110/3*(32.5**2+32.5*12.5+12.5**2)"}))
    import json as _json
    data = _json.loads(result)
    assert data.get("success") is True, f"calculate 失败: {data}"
    assert abs(float(data["numeric"]) - 186466.606) < 1.0, f"截锥体计算不精确: {data}"


def test_t9b_calculate_rejects_injection_and_unknown_symbols():
    import app.tools.calculate  # noqa: F401
    from app.tools.registry import registry
    import json as _json

    entry = registry.get_entry("calculate")
    bad = asyncio.run(entry.handler({"expression": "().__import__('os').system('id')"}))
    assert _json.loads(bad).get("success") is False, "注入表达式未被拒绝"

    unknown = asyncio.run(entry.handler({"expression": "foo*2"}))
    assert _json.loads(unknown).get("success") is False, "未知符号未被拒绝"


# ── T10: prompt/template upgrades (acceptance #10 / P1 提示词三处) ──
def test_t10a_execute_code_description_rewrite():
    from types import SimpleNamespace
    from app.services.agent_service import AgentService

    svc = AgentService.__new__(AgentService)
    shared = SimpleNamespace(memory_summary="", dream_summary="", memory_entries=[])
    workspace = SimpleNamespace(root_path="/tmp/x")
    prompt = asyncio.run(
        svc._build_system_prompt(assistant=None, shared_context=shared, workspace=workspace, user=None)
    )
    # 旧描述已删除：「仅当…复杂计算」不再作为 execute_code 的使用门槛
    assert "或需要复杂计算/数据处理时使用" not in prompt, "execute_code 描述仍保留'仅当复杂计算'旧门槛"
    # 新描述存在：推导数值一律工具计算 + 打印公式与中间量
    assert "推导数值" in prompt and ("execute_code" in prompt or "calculate" in prompt), "缺少'推导数值一律工具计算'新描述"
    assert "calculate" in prompt, "系统提示未介绍 calculate 工具"
    assert "中间量" in prompt, "execute_code 描述缺少'打印公式与中间量'纪律"


def test_t10b_auditor_template_b7_boundary_and_ungrounded_clause():
    t = AgentLoop._AUDITOR_SYSTEM_TEMPLATE
    # B7 边界：审计员可重算，但重算结果只能支撑 needs_evidence，不得作为 reject 依据
    ok_b7 = "execute_code" in t and "needs_evidence" in t and "不得" in t
    assert ok_b7, "审计模板缺少 B7 重算边界条款"
    # 无回执推导数值 → needs_evidence 条款
    assert ("回执" in t or "工具计算" in t) and "needs_evidence" in t, "审计模板缺少无回执推导数值条款"


# ── T20: 标识符数字豁免（conv 6871cf68，2026-10-17 生产误伤） ─────────────
# 生产事故：草稿照录 12306 查询结果（表格行含 D2281），NPG 把标识符中的
# 「2281」当「未经计算回执的推导数值」打回（_BARE_RE 后瞻只排数字/点，不排
# 字母）；execute_code 校核输出含 2281 → 进推导闭包变得可 flag，而 B1a/B1b
# 回执规则永不给标识符接地 → 不可满足打回 → 退化出货。豁免=后瞻加 ASCII 字母。
TRAVEL_USER_MSG = "从南京去香港最便宜的路费怎么走，我看高铁要971"
TRAVEL_DRAFT = (
    "| 车次 | 终到站 | 时刻 | 历时 | 二等座 |\n"
    "|---|---|---|---|---|\n"
    "| G2789 | 深圳 | 14:52 - 22:35 | 7 小时 43 分 | 695 元 |\n"
    "| D2281 | 深圳北 | 06:32 - 20:14 | 13 小时 42 分 | 818 元 |\n"
    "| G385 | 深圳北 | 10:25 - 17:30 | 7 小时 05 分 | 830.5 元 |\n"
    "- 推荐 G2781：终到深圳站，15:26 到。T31 次硬座 219.5 元。\n"
)


def test_t20_identifier_numbers_not_gated():
    """车次/航班等字母前缀标识符（D2281/G385/T31/G2789/G2781）中的数字
    不是数量候选——即便证据窗缺失且闭包含同值（校核脚本输出），也不得 flag。"""
    r = _report(
        TRAVEL_DRAFT,
        evidence_text="",  # 证据窗被预算裁掉的极端情形（生产实况）
        user_messages=[TRAVEL_USER_MSG],
        tool_calls=[
            # 校核脚本：输出含标识符数字（进闭包），代码无溯源锚点（B1b 不接地）
            {"name": "execute_code", "code": "rows = draft_table.splitlines()\nprint(len(rows))",
             "output": "D2281 ok; G385 ok; T31 ok; 15/15"},
        ],
    )
    # 判别成员说明（评审 M3）：集合中 31 不过 risk 地板、2789/2781 不进闭包，
    # 改动前后都永不 flag；真正有判别力的是 2281/385（闭包命中+B1b 无锚）。
    flagged = {f.raw for f in r.flags}
    assert not ({"2281", "385", "31", "2789", "2781"} & flagged), \
        f"标识符数字被误伤（conv 6871cf68 回归）: {r.flags}"


def test_t20b_plain_quantity_still_gated():
    """对照钉死豁免边界：闭包命中的纯数量形态（无字母前缀、无接地）仍必须
    flag——豁免只削标识符上下文，不放宽数量闸门（2026-10-18 收紧后触发面
    =数学块结果位∨闭包命中）。"""
    # 2026-10-18 收紧后对照改为闭包命中形态：1017=971+46（用户输入闭包
    # 配对和，非 B8 单步）、无接地 → 仍必须 flag（豁免只削标识符，数量闸门
    # 不放宽）。取值避开 19xx/20xx 年号形态（_YEAR_RE 预豁免）与 currency
    # 族（可导出排除清单内 unit'd 永不触发）——必须裸值。
    r = _report(
        "两种走法合计 1017。",
        evidence_text="",
        user_messages=[TRAVEL_USER_MSG, "港铁段约 46"],
        tool_calls=[],
    )
    assert any(abs(f.value - 1017.0) < 0.01 for f in r.flags), f"闭包命中纯数量应仍被 gated: {r.flags}"


def test_t20c_user_anchor_identifier_aligned():
    """评审 I1 回归形态：user_nums 锚点抽取（gate.py 内联正则）与 _BARE_RE
    同口径豁免字母紧邻数字——用户消息「坐 D2281」+ 草稿裸重述「车次 2281」
    不得 flag（修复前：quoted 语料接地；若锚点侧漏豁免则退化成不可满足打回）。"""
    r = _report(
        "好的，车次 2281 已为您记下，明天出发。",
        evidence_text="",
        user_messages=["我明天坐 D2281 去深圳"],
        tool_calls=[],
    )
    assert not any(f.raw == "2281" for f in r.flags), \
        f"用户消息标识符锚点未豁免导致裸重述被误伤: {r.flags}"


def test_t20d_identifier_with_unit_suffix_not_gated():
    """评审 A-M2 残留形态：标识符紧贴单位词（D2281 次列车）+ marker 邻接
    （合计）也不得是候选——_UNITED_RE 后瞻与 _BARE_RE 同口径排字母。"""
    r = _report(
        "本月合计开行 D2281 次列车，上座率不错。",
        evidence_text="",
        user_messages=[TRAVEL_USER_MSG],
        tool_calls=[],
    )
    assert not any(f.raw.startswith("2281") for f in r.flags), \
        f"标识符+单位词形态被误伤: {r.flags}"


def test_t21_untrusted_receipt_output_does_not_feed_closure():
    """4a 深层根治（2026-10-18，调研后方案）：闭包只吞通过 B1 溯源（代码含
    用户/证据锚点、非裸字面量表达式）的回执输出值。校核脚本（代码无锚点）
    打印的照录值不进闭包 → 不触发 → 不存在「照录事实被不可满足打回」。
    D2281 事故的纯数字形态回归（无标识符豁免可依赖）。"""
    # 不可信校核脚本：代码无锚点 → 输出 1075 不进闭包 → 草稿 1075 不触发
    r = _report(
        "两种走法相差 1075。",
        evidence_text="",
        user_messages=[TRAVEL_USER_MSG],
        tool_calls=[{"name": "execute_code",
                     "code": "rows = draft_table.splitlines()\nprint('check 1075 ok')",
                     "output": "1075 ok 15/15"}],
    )
    assert not any(abs(f.value - 1075.0) < 0.01 for f in r.flags), \
        f"不可信校核输出污染闭包导致照录值被误伤: {r.flags}"


def test_t21b_trusted_receipt_output_feeds_closure():
    """对照：可信回执（代码含锚点 971）的输出 1075 进闭包——草稿写其推导值
    2150=1075×2（未被打印、无接地）→ 仍 flag（闭包触发面不萎缩）。"""
    r = _report(
        "两种走法相差 2150。",
        evidence_text="",
        user_messages=[TRAVEL_USER_MSG],
        tool_calls=[{"name": "execute_code",
                     "code": "base = 971\nprint(base + 104)",
                     "output": "1075"}],
    )
    assert any(abs(f.value - 2150.0) < 0.01 for f in r.flags), \
        f"可信回执的闭包推导值应仍被 gated: {r.flags}"


def test_t21c_anchor_leg_is_load_bearing():
    """评审补缺（2026-10-18）：锚点腿是白名单的承重腿——校核脚本代码既不含
    被校核值字面量、也无溯源锚点（print(len(rows)) 形态），其输出照录值不得
    进闭包 → 草稿照录该值不触发（conv 6871cf68 不可满足打回的原形回归）。
    若删锚点腿（any(_num_in_code...)），本测试即 RED。"""
    r = _report(
        "两种走法相差 1075。",
        evidence_text="",
        user_messages=[TRAVEL_USER_MSG],
        tool_calls=[{"name": "execute_code",
                     "code": "rows = draft_table.splitlines()\nprint(len(rows), 'rows ok')",
                     "output": "diff total 1075; 15 rows ok"}],
    )
    assert not any(abs(f.value - 1075.0) < 0.01 for f in r.flags), \
        f"无锚点校核脚本的照录值进闭包（锚点腿失效）: {r.flags}"
