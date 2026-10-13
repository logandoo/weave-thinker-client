"""conv a8290d37 (2026-08-28): 行动层/决策层冲突仲裁 + doom 窗口熔断 回归测试。

事故：模型每次响应 = 完整正式回答 content + 冗余 browser tool_call；
框架「有 tool_calls → content 为过渡文本」丢弃了 ~11 份答案（stream buffer
33471 字 vs 落库 2857 字），发送前审计结构性失明；doom 检测只认连续 3 次
同参，被 URL 变体交替（A,B,A,B,A）绕过，触发后也无升级动作。
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.agent_loop import (  # noqa: E402
    AgentLoop,
    AgentLoopState,
    _calls_all_redundant,
    _detect_doom_loop,
    _is_redundant_call,
    _normalize_fetch_urls,
    _record_tool_success,
    _should_promote_answer_over_tools,
)


def _tc(name: str, args: dict) -> dict:
    return {"id": "call_x", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


# ── 冗余判据 (_is_redundant_call) ────────────────────────────────

def test_redundant_exact_duplicate_sig():
    state = AgentLoopState()
    args = {"queries": ["a", "b"]}
    _record_tool_success(state, "web_search", args, json.dumps({"results": [1]}))
    assert _is_redundant_call(state, _tc("web_search", args)) is True


def test_redundant_browser_urls_subset():
    state = AgentLoopState()
    state._fetched_urls.update(["https://a.com/x", "https://b.com/y"])
    assert _is_redundant_call(state, _tc("browser", {"urls": ["https://a.com/x"]})) is True
    assert _is_redundant_call(
        state, _tc("browser", {"urls": ["https://a.com/x", "https://b.com/y"]})) is True


def test_paginated_browser_never_redundant():
    """paginated=true 是更深抓取意图（同 URL 不同分页），绝不算冗余。"""
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    assert _is_redundant_call(
        state, _tc("browser", {"urls": ["https://a.com/x"], "paginated": True, "max_pages": 5})
    ) is False


def test_new_url_not_redundant():
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    assert _is_redundant_call(state, _tc("browser", {"urls": ["https://new.com/"]})) is False
    # 部分新 URL 也不算冗余
    assert _is_redundant_call(
        state, _tc("browser", {"urls": ["https://a.com/x", "https://new.com/"]})) is False


def test_unparseable_args_not_redundant():
    state = AgentLoopState()
    bad = {"id": "c", "type": "function", "function": {"name": "browser", "arguments": "{broken"}}
    assert _is_redundant_call(state, bad) is False


def test_failed_call_not_redundant():
    """错误载荷不算成功调用——同参重试不得被当冗余。"""
    state = AgentLoopState()
    args = {"urls": ["https://a.com/x"]}
    _record_tool_success(state, "browser", args, json.dumps({"error": "No URLs provided"}))
    assert _is_redundant_call(state, _tc("browser", args)) is False


def test_calls_all_redundant_mixed():
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    calls = [_tc("browser", {"urls": ["https://a.com/x"]}), _tc("browser", {"urls": ["https://new.com/"]})]
    assert _calls_all_redundant(state, calls) is False
    assert _calls_all_redundant(state, calls[:1]) is True


# ── 成功记录 (_record_tool_success) ──────────────────────────────

def test_record_browser_pages_with_text_only():
    state = AgentLoopState()
    payload = {"url_count": 2, "pages": [
        {"url": "https://a.com/ok", "title": "t", "text": "content", "error": None},
        {"url": "https://a.com/404", "title": "", "text": "", "error": "http 404"},
    ]}
    _record_tool_success(state, "browser", {"urls": ["u"]}, json.dumps(payload))
    assert state._fetched_urls == {"https://a.com/ok"}


def test_record_non_browser_sig_only():
    state = AgentLoopState()
    _record_tool_success(state, "web_search", {"queries": ["q"]}, json.dumps({"results": []}))
    assert len(state._succeeded_call_sigs) == 1
    assert state._fetched_urls == set()


# ── doom 窗口检测 (_detect_doom_loop) ────────────────────────────

def test_doom_consecutive_three():
    h = [("browser", "hA"), ("browser", "hA"), ("browser", "hA")]
    assert _detect_doom_loop(h) == ("browser", "hA")


def test_doom_alternating_variants():
    """conv a8290d37 逃逸模式：A,B,A,B,A 交替 —— 旧连续-3 判据免疫，窗口判据必须抓到。"""
    h = [("browser", "hA"), ("browser", "hB"), ("browser", "hA"), ("browser", "hB"), ("browser", "hA")]
    assert _detect_doom_loop(h) == ("browser", "hA")


def test_doom_no_false_positive():
    h = [("browser", "hA"), ("browser", "hB"), ("browser", "hA"), ("browser", "hC"), ("browser", "hB"), ("browser", "hD")]
    assert _detect_doom_loop(h) is None


def test_doom_window_only_last_six():
    """窗口外的历史不计：前 3 个 A 已滚出窗口，近 6 次无 ≥3 同参。"""
    h = [("browser", "hA")] * 3 + [("browser", "hB"), ("browser", "hC"), ("browser", "hD"),
                                    ("browser", "hE"), ("browser", "hF"), ("browser", "hG")]
    assert _detect_doom_loop(h) is None


# ── 仲裁判定 (_should_promote_answer_over_tools) ─────────────────

def test_promote_substantial_answer_all_redundant():
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    content = "完整回答" * 300  # ≥800 chars
    calls = [_tc("browser", {"urls": ["https://a.com/x"]})]
    assert _should_promote_answer_over_tools(state, content, calls, min_chars=800) is True


def test_no_promote_short_transitional_prose():
    """简短过渡文字 + 工具调用 = 合法流程，不仲裁。"""
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    calls = [_tc("browser", {"urls": ["https://a.com/x"]})]
    assert _should_promote_answer_over_tools(state, "让我再确认一下", calls, min_chars=800) is False


def test_no_promote_when_any_call_new():
    state = AgentLoopState()
    state._fetched_urls.add("https://a.com/x")
    content = "完整回答" * 300
    calls = [_tc("browser", {"urls": ["https://a.com/x"]}), _tc("browser", {"urls": ["https://new.com/"]})]
    assert _should_promote_answer_over_tools(state, content, calls, min_chars=800) is False


def test_no_promote_empty_calls():
    state = AgentLoopState()
    assert _should_promote_answer_over_tools(state, "完整回答" * 300, [], min_chars=800) is False


# ── doom 触发即熔断（force_final_answer）─────────────────────────

def _make_loop(deathmatch: bool = False) -> AgentLoop:
    dm = None
    if deathmatch:
        class _DM:
            is_goal_active = True
            is_active = True
        dm = _DM()
    return AgentLoop(llm=object(), tool_schemas=[], deathmatch_manager=dm)


def test_doom_trigger_forces_final_answer():
    loop = _make_loop()
    state = AgentLoopState()
    # 预置交替历史（差一次即触发窗口 doom）
    state._doom_tool_history.extend([
        ("browser", "hA"), ("browser", "hB"), ("browser", "hA"), ("browser", "hB"),
    ])
    args = {"urls": ["https://a.com/x"]}
    # 该调用的签名须等于 hA：hA 是任意占位，直接算真实签名放进去
    from app.services.agent_loop import _call_signature
    sig = _call_signature("browser", args)
    state._doom_tool_history[0] = sig
    state._doom_tool_history[2] = sig
    result = asyncio.run(
        loop._execute_single_tool("c1", "browser", args, None, None, None, None, state)
    )
    assert result.error is True
    assert "Doom loop" in result.result
    assert state.force_final_answer is True


def test_doom_no_force_in_deathmatch():
    loop = _make_loop(deathmatch=True)
    state = AgentLoopState()
    from app.services.agent_loop import _call_signature
    args = {"urls": ["https://a.com/x"]}
    sig = _call_signature("browser", args)
    state._doom_tool_history.extend([sig, ("browser", "hB"), sig, ("browser", "hB")])
    result = asyncio.run(
        loop._execute_single_tool("c1", "browser", args, None, None, None, None, state)
    )
    assert result.error is True  # doom 仍拦截本次调用
    assert state.force_final_answer is False  # 但 deathmatch 不强制收尾


def test_normalize_fetch_urls():
    assert _normalize_fetch_urls({"urls": " https://a.com "}) == ["https://a.com"]
    assert _normalize_fetch_urls({"urls": ["https://a.com", "", None, "https://b.com"]}) == ["https://a.com", "https://b.com"]
    assert _normalize_fetch_urls({}) == []


# ── A4.9 R1 Important #1: browser 页面级失败不得记签名 ─────────────

def test_browser_page_level_failure_not_recorded():
    """browser 几乎从不返回顶层 error：全页失败是 200 形 {"pages":[{"text":"","error":...}]}。
    这种"成功返回的语义失败"不得记签名——同参重试（可能是瞬时限流）不是冗余。"""
    state = AgentLoopState()
    payload = {"url_count": 1, "pages": [{"url": "https://a.com/x", "title": "", "text": "", "error": "http 404"}]}
    _record_tool_success(state, "browser", {"urls": ["https://a.com/x"]}, json.dumps(payload))
    assert state._fetched_urls == set()
    assert state._succeeded_call_sigs == set()
    assert _is_redundant_call(state, _tc("browser", {"urls": ["https://a.com/x"]})) is False


def test_browser_partial_success_recorded():
    """部分页面有文本 → 调用有效，签名可记（同参纯重复即冗余）。"""
    state = AgentLoopState()
    payload = {"url_count": 2, "pages": [
        {"url": "https://a.com/ok", "title": "t", "text": "content", "error": None},
        {"url": "https://a.com/404", "title": "", "text": "", "error": "http 404"},
    ]}
    _record_tool_success(state, "browser", {"urls": ["https://a.com/ok", "https://a.com/404"]}, json.dumps(payload))
    assert state._fetched_urls == {"https://a.com/ok"}
    assert len(state._succeeded_call_sigs) == 1


# ── A4.9 R1 Important #2: doom 结果感知（轮询豁免）─────────────────

def test_doom_exempts_polling_fresh_results():
    """同 URL 轮询但每次返回新数据 = 健康监控流，不得判 doom。"""
    from app.services.agent_loop import _call_signature
    sig = _call_signature("browser", {"urls": ["https://status.example"]})
    history = [sig, ("execute_code", "hX"), sig, ("browser", "hY"), sig]
    results = {sig: ["hashR1", "hashR2", "hashR3"]}  # 每次结果都不同
    assert _detect_doom_loop(history, results) is None


def test_doom_catches_identical_results():
    """事故模式：同参重复且结果逐字节相同（静态页重复抓取）→ doom。"""
    from app.services.agent_loop import _call_signature
    sigA = _call_signature("browser", {"urls": ["https://a.com"]})
    sigB = _call_signature("browser", {"urls": ["https://b.com"]})
    history = [sigA, sigB, sigA, sigB, sigA]
    results = {sigA: ["hashR", "hashR"], sigB: ["hashQ"]}
    assert _detect_doom_loop(history, results) == sigA


def test_doom_zero_results_still_doom():
    """无任何结果记录（全部预调度失败）→ 无法证明新鲜，保守判 doom（旧语义）。"""
    sig = ("browser", "hA")
    assert _detect_doom_loop([sig, sig, sig], {}) == sig


def test_doom_single_result_treated_stale():
    """仅 1 条结果记录（其余为 doom 中止的预调度调用）→ 维持 doom（与事故 call 12-15 行为一致）。"""
    from app.services.agent_loop import _call_signature
    sig = _call_signature("browser", {"urls": ["https://a.com"]})
    assert _detect_doom_loop([sig, sig, sig], {sig: ["hashR"]}) == sig


# ── A4.9 R1 Minor #3: 签名在参数归一化前后必须一致 ────────────────

def test_sig_consistent_across_arg_coercion():
    """模型原始 args（"max_pages":"5" 字符串）与归一化后（int 5）签名必须一致，
    否则 exact-duplicate 判据对可归一化参数静默失效。"""
    from app.services.agent_loop import _call_signature
    raw = {"urls": ["https://a.com"], "max_pages": "5"}
    coerced = {"urls": ["https://a.com"], "max_pages": 5}
    assert _call_signature("browser", raw) == _call_signature("browser", coerced)


# ── A4.9 R1 Minor #4: doom 文案与窗口判据一致 ─────────────────────

def test_doom_message_matches_window_semantics():
    loop = _make_loop()
    state = AgentLoopState()
    from app.services.agent_loop import _call_signature
    args = {"urls": ["https://a.com/x"]}
    sig = _call_signature("browser", args)
    state._doom_tool_history.extend([sig, sig])
    result = asyncio.run(
        loop._execute_single_tool("c1", "browser", args, None, None, None, None, state)
    )
    assert result.error is True
    assert "last 6" in result.result or "6" in result.result
    assert "consecutive times with identical arguments. Aborting" not in result.result


# ── A4.9 R1 Minor #6: force 阶段非 execute_code 原生调用须被丢弃 ──

def test_force_stage_drops_non_execute_code_calls():
    """熔断器硬停止：force_final_answer 阶段模型幻觉出的原生 browser 调用
    （事故中模型 4 次无视 abort 的通道）必须被丢弃，不得执行。"""
    from app.services.agent_loop import _filter_force_stage_calls
    state = AgentLoopState()
    state.force_final_answer = True
    calls = [_tc("browser", {"urls": ["https://a.com"]}), _tc("execute_code", {"code": "print(1)"})]
    kept, dropped = _filter_force_stage_calls(state, calls)
    assert [c["function"]["name"] for c in kept] == ["execute_code"]
    assert [c["function"]["name"] for c in dropped] == ["browser"]
    # 非 force 阶段不过滤
    state.force_final_answer = False
    kept2, dropped2 = _filter_force_stage_calls(state, calls)
    assert len(kept2) == 2 and not dropped2
