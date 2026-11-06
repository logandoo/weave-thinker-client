"""C-01..C-04 move-only contracts (2026-10-19).

These pin the module boundaries and public signatures without duplicating the
behavioural suites: the loop/transport/mixins must live in their new modules and
the host files must not grow back.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _lines(rel: str) -> int:
    return (ROOT / rel).read_text(encoding="utf-8").count("\n") + 1


def test_run_loop_moved_to_orchestrator():
    from app.services.agent_loop import AgentLoop
    from app.services.agent_loop_orchestrator import AgentLoopOrchestratorMixin

    assert issubclass(AgentLoop, AgentLoopOrchestratorMixin)
    assert AgentLoop._run_loop.__module__ == "app.services.agent_loop_orchestrator"
    params = list(inspect.signature(AgentLoop._run_loop).parameters)
    assert params == ["self", "state", "session_factory", "user", "conversation", "assistant"]


def test_chat_stream_route_delegates_to_transport():
    import app.api.chat as chat
    import app.api.chat_stream_transport as transport

    route_src = inspect.getsource(chat.chat_stream)
    assert "stream_chat_response" in route_src
    assert "event_generator" not in route_src
    params = list(inspect.signature(transport.stream_chat_response).parameters)
    assert params == ["request", "req", "db", "current_user"]
    assert "async def _chat_event_generator" in inspect.getsource(transport)


def test_host_files_stay_bounded():
    assert _lines("app/services/agent_loop.py") <= 8000
    assert _lines("app/api/chat.py") <= 2000
    assert _lines("app/services/deathmatch_service.py") <= 2500
    assert _lines("app/services/voice_service.py") <= 1800


def test_deathmatch_mixins_are_in_mro():
    from app.services.deathmatch_service import DeathmatchManager
    from app.services.deathmatch_status import DeathmatchStatusMixin
    from app.services.deathmatch_attachments import DeathmatchAttachmentsMixin
    from app.services.deathmatch_workspace import DeathmatchWorkspaceMixin
    from app.services.deathmatch_evidence import DeathmatchEvidenceMixin
    from app.services.deathmatch_grilling_flow import DeathmatchGrillingFlowMixin
    from app.services.deathmatch_plan_core import DeathmatchPlanCoreMixin

    assert issubclass(DeathmatchManager, DeathmatchStatusMixin)
    assert issubclass(DeathmatchManager, DeathmatchAttachmentsMixin)
    assert issubclass(DeathmatchManager, DeathmatchWorkspaceMixin)
    assert issubclass(DeathmatchManager, DeathmatchEvidenceMixin)
    assert issubclass(DeathmatchManager, DeathmatchGrillingFlowMixin)
    assert issubclass(DeathmatchManager, DeathmatchPlanCoreMixin)
    for name in ("resume", "begin_revision", "_handle_stall", "get_verdict_dict"):
        assert getattr(DeathmatchManager, name).__module__ == "app.services.deathmatch_status"
    for name in ("collect_final_deliverables_from_messages",
                 "generate_final_summary_table"):
        assert getattr(DeathmatchManager, name).__module__ == "app.services.deathmatch_attachments"
    for name in ("generate_grilling_questions", "submit_grilling_round",
                 "try_recover_stalled_grilling"):
        assert getattr(DeathmatchManager, name).__module__ == "app.services.deathmatch_grilling_flow"
    for name in ("_build_judge_evidence", "build_context_messages",
                 "compress_conversation_context"):
        assert getattr(DeathmatchManager, name).__module__ == "app.services.deathmatch_plan_core"


def test_voice_mixins_are_in_mro():
    from app.services.voice_service import VoiceDuplexSession
    from app.services.voice_tts import VoiceTTSMixin
    from app.services.voice_asr_session import VoiceASRSessionMixin
    from app.services.voice_turn import VoiceTurnMixin
    from app.services.voice_session_setup import VoiceSessionSetupMixin
    from app.services.voice_memory import VoiceMemoryMixin
    from app.services.voice_filler import VoiceFillerMixin
    from app.services.voice_ws import VoiceWSMixin

    for mixin in (VoiceTTSMixin, VoiceASRSessionMixin, VoiceTurnMixin,
                  VoiceSessionSetupMixin, VoiceMemoryMixin, VoiceFillerMixin,
                  VoiceWSMixin):
        assert issubclass(VoiceDuplexSession, mixin)
    assert VoiceDuplexSession._tts_consumer.__module__ == "app.services.voice_tts"
    assert VoiceDuplexSession._asr_pipeline.__module__ == "app.services.voice_asr_session"
    assert VoiceDuplexSession._on_user_turn_locked.__module__ == "app.services.voice_turn"
    assert VoiceDuplexSession._load_history.__module__ == "app.services.voice_session_setup"
    assert VoiceDuplexSession._vmem_maybe_judge.__module__ == "app.services.voice_memory"
    assert VoiceDuplexSession.run.__module__ == "app.services.voice_ws"
    assert VoiceDuplexSession._send_json.__module__ == "app.services.voice_ws"
    assert VoiceDuplexSession._maybe_backchannel.__module__ == "app.services.voice_filler"


def test_globals_sync_lists_resolve():
    import app.api.chat as chat
    import app.api.chat_stream_transport as transport
    import app.services.agent_loop as loop
    import app.services.agent_loop_orchestrator as orchestrator

    # The sync lists also contain local imports/builtins by design (harmless);
    # these representative module globals must resolve from the host module.
    for name in ("config", "logger", "_inject_directive", "ToolCallResult",
                 "_resolve_context_length", "_strip_dsml_streaming"):
        assert hasattr(loop, name), name
    for name in ("_agent_registry", "agent_service", "stream_buffer_manager",
                 "_load_conversation_messages", "_trim_messages_for_edit"):
        assert hasattr(chat, name), name
    # The sync functions themselves must be callable without raising.
    orchestrator._sync_agent_globals()
    transport._sync_chat_globals()


def _strip_sync(body):
    out = []
    for st in body:
        if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                and isinstance(st.value.func, ast.Name)
                and st.value.func.id.startswith("_sync_")
                and st.value.func.id.endswith("_globals")):
            continue
        out.append(st)
    return out


def _body_hash(fn) -> str:
    module = ast.Module(body=_strip_sync(fn.body), type_ignores=[])
    return hashlib.sha256(ast.dump(module, include_attributes=False).encode()).hexdigest()


def _find_fn(path: str, name: str):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_moved_bodies_are_ast_identical_to_baseline():
    manifest = json.loads(
        (Path(__file__).with_name("refactor_move_manifest_20261019.json"))
        .read_text(encoding="utf-8"))
    modules = (
        "app.services.deathmatch_status", "app.services.deathmatch_attachments",
        "app.services.deathmatch_workspace", "app.services.deathmatch_evidence",
        "app.services.voice_tts", "app.services.voice_asr_session",
        "app.services.voice_turn", "app.services.voice_session_setup",
        "app.services.voice_memory",
    )
    located = {}
    for modname in modules:
        mod = importlib.import_module(modname)
        path = Path(mod.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                located[node.name] = (str(path), node)
    for name, info in manifest["methods"].items():
        assert name in located, f"moved method not found: {name}"
        _, node = located[name]
        assert _body_hash(node) == info["body_hash"], f"move-only drift: {name}"
        assert [a.arg for a in node.args.args] == info["args"], f"signature drift: {name}"
        assert [ast.dump(d, include_attributes=False)
                for d in node.decorator_list] == info["decorators"], \
            f"decorator drift: {name}"

    from app.services.agent_loop import AgentLoop
    loop_node = _find_fn(inspect.getsourcefile(AgentLoop._run_loop), "_run_loop")
    assert loop_node.end_lineno - loop_node.lineno + 1 <= 1000
    assert [a.arg for a in loop_node.args.args] == manifest["loop"]["args"]
    fin_node = _find_fn(inspect.getsourcefile(AgentLoop._finalize_answer_block),
                        "_finalize_answer_block")
    assert fin_node.end_lineno - fin_node.lineno + 1 <= 300
    # C-01c: the primary-audit branch and its extracted segments stay bounded.
    for name, limit in (("_finalize_primary_audit_branch", 200),
                        ("_primary_audit_guidance", 200),
                        ("_primary_audit_verdict", 200),
                        ("_finalize_empty_answer_branch", 200)):
        node = _find_fn(inspect.getsourcefile(AgentLoop._finalize_answer_block), name)
        size = node.end_lineno - node.lineno + 1
        assert size <= limit, f"{name} regrew to {size} lines (limit {limit})"

    import app.api.chat_stream_transport as transport
    chat_node = _find_fn(inspect.getsourcefile(transport.stream_chat_response),
                         "stream_chat_response")
    assert chat_node.end_lineno - chat_node.lineno + 1 <= 900
    assert [a.arg for a in chat_node.args.args] == manifest["chat"]["args"]
    ev_node = _find_fn(inspect.getsourcefile(transport._chat_event_generator),
                       "_chat_event_generator")
    assert ev_node.end_lineno - ev_node.lineno + 1 <= 500


def test_agent_loop_transport_decomposition_bounds():
    """C-02c: the agent-loop run is split into bounded module-level helpers."""
    import app.api.chat_stream_transport as transport

    limits = {
        "_chat_agent_loop_impl": 800,
        "_chat_run_tool_loop": 1200,
        "_drive_agent_events": 500,
        "_finalize_agent_run": 500,
        "_handle_dm_verdict_event": 500,
        "_handle_agent_loop_cancel": 300,
    }
    path = inspect.getsourcefile(transport._chat_agent_loop_impl)
    for name, limit in limits.items():
        node = _find_fn(path, name)
        size = node.end_lineno - node.lineno + 1
        assert size <= limit, f"{name} regrew to {size} lines (limit {limit})"
        assert getattr(transport, name).__module__ == "app.api.chat_stream_transport"

    impl = inspect.getsource(transport._chat_agent_loop_impl)
    assert "await _drive_agent_events(run)" in impl
    assert "await _finalize_agent_run(run)" in impl
    assert "await _handle_agent_loop_cancel(run)" in impl
    driver = inspect.getsource(transport._drive_agent_events)
    assert '_handle_dm_verdict_event(run, event["deathmatch_verdict"])' in driver
    # the kw bridge must be built at the point of use, not at def time
    caller_loop = inspect.getsource(transport._chat_run_tool_loop)
    tramp = caller_loop[caller_loop.index("async def _agent_loop_impl()"):
                        caller_loop.index("async def _agent_loop_task()")]
    assert "_chat_agent_loop_impl(_state, **{" in tramp
    assert "_agent_loop_kw" not in caller_loop
    # every accumulator the split helpers mutate lives on the shared run state
    for name in ("assistant_content", "display_sequence", "tool_results_accumulated",
                 "dm_sync_snapshot", "pre_tool_gate", "pf", "put", "dm"):
        assert name in transport._AgentLoopRun.__dataclass_fields__, name


def test_run_context_bound_before_grilling_and_finalize():
    """C-02c regression: the run context must be bound BEFORE the grilling
    branch — otherwise the grilling path reaches `_finalize_agent_run` with
    `run.state`/`run.dm` still at their `None` defaults (review Important-2)."""
    import app.api.chat_stream_transport as transport

    impl = inspect.getsource(transport._chat_agent_loop_impl)
    bind = impl.index("run.state = _state")
    bind_last = impl.index("run.persist_final_answer = _persist_final_answer")
    grilling = impl.index(
        "if _deathmatch_mgr and _deathmatch_mgr.is_grilling and _grilling_generated:")
    driver = impl.index("await _drive_agent_events(run)")
    finalize = impl.index("await _finalize_agent_run(run)")
    assert bind < bind_last < grilling < driver < finalize

    # EVERY field read by the extracted helpers must be assigned BEFORE the
    # grilling branch (a middle field drifting back into the `else:` is the
    # exact regression this test exists for).
    fields = ("state", "agent_state", "agent_queue", "dm", "put",
              "conversation_id", "cap_conversation_id", "cap_assistant",
              "user_id_local", "username_local", "ctx_window", "agent_loop",
              "messages_for_model", "task_user", "task_conversation",
              "coord_result", "conversation_messages", "ttft_t0",
              "strip_canary_streaming", "sse_cancelled", "generate_title_bg",
              "persist_final_answer")
    for field in fields:
        at = impl.index(f"run.{field} =")
        assert bind <= at <= bind_last, f"run.{field} bound outside the prologue"
        assert at < grilling, f"run.{field} bound after the grilling branch"

    # liveness: every right-hand side is assigned before the bind point, so a
    # future edit cannot move an assignment into a skippable branch.
    for local in ("_agent_state", "_agent_queue", "_deathmatch_mgr", "_put",
                  "conversation_id", "_cap_conversation_id", "_cap_assistant",
                  "_user_id_local", "_username_local", "_ctx_window", "agent_loop",
                  "messages_for_model", "task_user", "task_conversation",
                  "coord_result", "conversation_messages", "_ttft_t0",
                  "strip_canary_streaming", "_sse_cancelled", "_generate_title_bg",
                  "_persist_final_answer"):
        starts = [i for i in (impl.find(f"{local} = "),
                              impl.find(f"def {local}("),
                              impl.find(f"def {local} (")) if i >= 0]
        assert starts, f"{local} is never bound in the impl"
        assert min(starts) < bind, f"{local} bound after the run-context bind point"


def test_agent_impl_has_no_closure_cells():
    """C-02c: `_chat_agent_loop_impl` must take everything via **kw.

    A new free variable means a closure cell leaked back into the hoisted
    function (the failure mode the hoist was meant to remove), and the
    `_agent_loop_kw` call-site dict must supply every `kw.get(...)` key.
    """
    import re
    import symtable

    import app.api.chat_stream_transport as transport

    path = inspect.getsourcefile(transport._chat_agent_loop_impl)
    source = Path(path).read_text(encoding="utf-8")
    table = symtable.symtable(source, path, "exec")

    def walk(node):
        for child in node.get_children():
            if child.get_name() == "_chat_agent_loop_impl":
                return child
            found = walk(child)
            if found is not None:
                return found
        return None

    impl_table = walk(table)
    assert impl_table is not None
    # A module-level function can never own free variables, so `free == set()`
    # alone is tautological; the meaningful invariant is that every name the
    # impl resolves as a GLOBAL actually exists (typo / deleted helper gate).
    import builtins

    import app.api.chat as chat
    unresolved = sorted(
        name for name in impl_table.get_globals()
        if not hasattr(builtins, name)
        and not hasattr(chat, name) and name not in transport.__dict__
    )
    assert not unresolved, f"impl references undefined globals: {unresolved}"

    keys = set(re.findall(r"= kw\.get\('([^']+)'\)",
                          inspect.getsource(transport._chat_agent_loop_impl)))
    assert len(keys) >= 19, f"kw contract shrank unexpectedly: {len(keys)}"
    # `_state` is the positional primary object, never a kw key (no shadowing)
    assert "_state" not in keys
    caller = inspect.getsource(transport._chat_run_tool_loop)
    missing = {k for k in keys if f"'{k}': {k}" not in caller}
    assert not missing, f"_agent_loop_kw is missing bound frees: {sorted(missing)}"


def _self_call_arity_problems(cls):
    """Static arity check for `self.<method>(...)` calls in one class body.

    Compares positional count AND required keyword-only names against the
    same-class definition.  Calls using `*args` / `**kwargs` packing, variadic
    definitions, and methods not defined in this class body are skipped (the
    latter are covered by the mixin MRO contracts).  Returns human-readable
    problem strings.
    """
    defs = {}
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        positional = a.posonlyargs + a.args
        defs[node.name] = {
            "required_pos": max(len(positional) - len(a.defaults) - 1, 0),
            "required_kwonly": [arg.arg for arg, d in zip(a.kwonlyargs, a.kw_defaults)
                                if d is None],
            "max_pos": None if a.vararg else len(positional) - 1 + len(a.kwonlyargs),
        }
    problems = []
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "self"):
            continue
        spec = defs.get(fn.attr)
        if spec is None:
            continue
        if any(isinstance(a, ast.Starred) for a in node.args):
            continue
        if any(k.arg is None for k in node.keywords):   # **kwargs packing
            continue
        got = len(node.args)
        if got < spec["required_pos"]:
            problems.append(
                f"{fn.attr}: {got} positional args < required {spec['required_pos']}"
                f" (line {node.lineno})")
        elif spec["max_pos"] is not None and got > spec["max_pos"]:
            problems.append(
                f"{fn.attr}: {got} positional args > max {spec['max_pos']}"
                f" (line {node.lineno})")
        passed_kw = {k.arg for k in node.keywords}
        missing = [n for n in spec["required_kwonly"] if n not in passed_kw]
        if missing:
            problems.append(
                f"{fn.attr}: missing required keyword-only {missing} (line {node.lineno})")
    return problems


def test_self_call_arity_checker_semantics():
    """The gate must flag real mismatches without false reds."""
    import ast as _ast
    import textwrap as _textwrap

    src = _textwrap.dedent("""
        class C:
            def ok(self, a, b):
                return a + b
            def needs_kw(self, a, *, flag):
                return a
            def variadic(self, *args):
                return args
            def calls(self, x):
                self.ok(x, 1)
                self.needs_kw(x, flag=True)
                self.variadic(x, 1, 2, 3)
                self.ok(*x)
                self.ok(**x)
                return None
    """)
    cls = _ast.parse(src).body[0]
    assert _self_call_arity_problems(cls) == []

    bad = _textwrap.dedent("""
        class C:
            def needs_two(self, a, b):
                return a
            def needs_kw(self, a, *, flag):
                return a
            def calls(self, x):
                self.needs_two(x)
                self.needs_kw(x)
                self.needs_kw(x, flag=True, extra=1)
                return None
    """)
    problems = _self_call_arity_problems(_ast.parse(bad).body[0])
    assert any("needs_two: 1 positional args" in p for p in problems), problems
    assert any("needs_kw: missing required keyword-only" in p for p in problems), problems
    assert not any("extra" in p for p in problems), problems


def test_self_call_arity_matches_definition():
    """C-01c regression: `_primary_audit_guidance` was called with 2 args while
    its definition took 4 — no behavioural test drives the audit branch, so the
    TypeError only surfaced in review.  This gate compares every same-class
    `self.<method>(...)` call against its definition."""
    import ast
    import inspect as _inspect
    from pathlib import Path as _P

    import app.services.agent_loop_orchestrator as orch

    path = _P(_inspect.getsourcefile(orch.AgentLoopOrchestratorMixin))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef)
                and n.name == "AgentLoopOrchestratorMixin"), None)
    assert cls is not None, "AgentLoopOrchestratorMixin not found at module top level"
    assert not _self_call_arity_problems(cls)


def test_self_call_arity_across_agent_loop_modules():
    """Same gate over the other large classes (AgentLoop + all orchestrator
    classes) so the defect class is covered beyond the one method that broke."""
    import ast
    import importlib as _importlib
    import inspect as _inspect
    from pathlib import Path as _P

    targets = [
        ("app.services.agent_loop_orchestrator", None),
        ("app.services.agent_loop", {"AgentLoop"}),
    ]
    problems = []
    for modname, only in targets:
        mod = _importlib.import_module(modname)
        tree = ast.parse(_P(_inspect.getsourcefile(mod)).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if only is not None and node.name not in only:
                continue
            for problem in _self_call_arity_problems(node):
                problems.append(f"{modname}.{node.name}: {problem}")
    assert not problems, problems


def test_audit_segments_write_through_loop_result():
    """The dropped finally write-back is safe only while the extracted segments
    mutate `_LoopResult` in place — pin that (a local + write-back would lose
    state on the `raise _LoopContinueSignal()` paths)."""
    import ast
    import inspect as _inspect
    import textwrap

    import app.services.agent_loop_orchestrator as orch

    shared = {
        "_adv_ev", "_adv_events", "_adv_text", "_audit_family",
        "_audit_reject_budget", "_audit_target", "_base_text", "_budget_spent",
        "_cov_note", "_family_stall", "_lc_note", "_npg_no_progress",
        # `_salvage_ev` is the per-iteration loop variable of the salvage
        # stream (transient, never read downstream) — not shared state.
        "_prev_stash", "_salvage_parts", "_shipped_guidance",
        "_shipped_text", "_ships_as_final", "_soft_limit", "_stitched_audit",
        "assistant_content", "guidance",
    }
    for name in ("_primary_audit_guidance", "_primary_audit_verdict",
                 "_finalize_primary_audit_branch"):
        src = textwrap.dedent(_inspect.getsource(getattr(orch.AgentLoopOrchestratorMixin, name)))
        fn = ast.parse(src).body[0]
        stores = {n.id for n in ast.walk(fn)
                  if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        leaked = sorted(stores & shared)
        assert not leaked, f"{name} keeps shared state in locals: {leaked}"


def test_finalize_branch_wiring_keeps_fresh_content_and_names():
    from app.services.agent_loop import AgentLoop

    caller = inspect.getsource(AgentLoop._finalize_answer_block)
    assert "'_final_content'" in caller
    assert "_finalize_synthesis_branch(" in caller
    synthesis = inspect.getsource(AgentLoop._finalize_synthesis_branch)
    assert "_final_content = kw.get('_final_content'" in synthesis
    canary = inspect.getsource(AgentLoop._finalize_canary_branch)
    assert "response_has_canary" in canary
    assert "_need_synthesis" not in canary


def _symtable_globals(path: str) -> set[str]:
    import symtable

    source = Path(path).read_text(encoding="utf-8")
    table = symtable.symtable(source, str(path), "exec")
    module_defined: set[str] = set()
    for name in table.get_identifiers():
        try:
            symbol = table.lookup(name)
        except KeyError:
            continue
        if symbol.is_assigned() or symbol.is_imported():
            module_defined.add(name)
    names: set[str] = set()

    def walk(node):
        if node.get_type() == "function":
            names.update(node.get_globals())
        for child in node.get_children():
            walk(child)

    walk(table)
    return names - module_defined


def _assert_sync_call(path: str, names: list[str], sync_name: str) -> None:
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    located = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in names:
        node = located[name]
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        first = body[0]
        assert (isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
                and isinstance(first.value.func, ast.Name)
                and first.value.func.id == sync_name), f"{name}: missing {sync_name}()"


def test_sync_lists_cover_all_host_globals_used_by_moved_code():
    import builtins

    import app.api.chat as chat
    import app.services.agent_loop as loop
    import app.services.deathmatch_service as dm
    import app.services.voice_service as voice
    import app.api.chat_stream_transport as transport
    import app.services.agent_loop_orchestrator as orchestrator
    import app.services.deathmatch_status as dm_status
    import app.services.deathmatch_attachments as dm_attach
    import app.services.deathmatch_workspace as dm_workspace
    import app.services.deathmatch_evidence as dm_evidence
    import app.services.voice_tts as vtt
    import app.services.voice_asr_session as vasr
    import app.services.voice_turn as vturn
    import app.services.voice_session_setup as vsetup
    import app.services.voice_memory as vmemory
    import app.services.deathmatch_grilling_flow as dm_grill
    import app.services.deathmatch_plan_core as dm_plan
    import app.services.voice_filler as vfiller
    import app.services.voice_ws as vws

    cases = [
        (transport.__file__, chat, transport._CHAT_GLOBALS),
        (orchestrator.__file__, loop, orchestrator._AGENT_GLOBALS),
        (dm_status.__file__, dm, dm_status._GLOBALS),
        (dm_attach.__file__, dm, dm_attach._GLOBALS),
        (dm_workspace.__file__, dm, dm_workspace._GLOBALS),
        (dm_evidence.__file__, dm, dm_evidence._GLOBALS),
        (vtt.__file__, voice, vtt._GLOBALS),
        (vasr.__file__, voice, vasr._GLOBALS),
        (vturn.__file__, voice, vturn._GLOBALS),
        (vsetup.__file__, voice, vsetup._GLOBALS),
        (vmemory.__file__, voice, vmemory._GLOBALS),
        (dm_grill.__file__, dm, dm_grill._GLOBALS),
        (dm_plan.__file__, dm, dm_plan._GLOBALS),
        (vfiller.__file__, voice, vfiller._GLOBALS),
        (vws.__file__, voice, vws._GLOBALS),
    ]
    for path, host, declared in cases:
        needed = {n for n in _symtable_globals(path)
                  if not hasattr(builtins, n)}
        missing_decl = sorted(needed - set(declared))
        missing_host = sorted(n for n in needed if not hasattr(host, n))
        assert not missing_decl, f"{path}: sync list missing globals {missing_decl}"
        assert not missing_host, f"{path}: unresolved global names {missing_host}"

    _assert_sync_call(transport.__file__, ["stream_chat_response"],
                      "_sync_chat_globals")
    _assert_sync_call(orchestrator.__file__, ["_run_loop"],
                      "_sync_agent_globals")
    _assert_sync_call(dm_status.__file__, list(
        n for n in dir(dm_status.DeathmatchStatusMixin)
        if not n.startswith("__")), "_sync_deathmatchstatus_globals")
    _assert_sync_call(dm_attach.__file__, list(
        n for n in dir(dm_attach.DeathmatchAttachmentsMixin)
        if not n.startswith("__")), "_sync_deathmatchattachments_globals")
    _assert_sync_call(vtt.__file__, list(
        n for n in dir(vtt.VoiceTTSMixin) if not n.startswith("__")),
        "_sync_voicetts_globals")
    _assert_sync_call(vasr.__file__, list(
        n for n in dir(vasr.VoiceASRSessionMixin) if not n.startswith("__")),
        "_sync_voiceasrsession_globals")
    _assert_sync_call(vturn.__file__, list(
        n for n in dir(vturn.VoiceTurnMixin) if not n.startswith("__")),
        "_sync_voiceturn_globals")
    _assert_sync_call(dm_workspace.__file__, list(
        n for n in dir(dm_workspace.DeathmatchWorkspaceMixin) if not n.startswith("__")),
        "_sync_deathmatchworkspace_globals")
    _assert_sync_call(dm_evidence.__file__, list(
        n for n in dir(dm_evidence.DeathmatchEvidenceMixin) if not n.startswith("__")),
        "_sync_deathmatchevidence_globals")
    _assert_sync_call(vsetup.__file__, list(
        n for n in dir(vsetup.VoiceSessionSetupMixin) if not n.startswith("__")),
        "_sync_voicesessionsetup_globals")
    _assert_sync_call(vmemory.__file__, list(
        n for n in dir(vmemory.VoiceMemoryMixin) if not n.startswith("__")),
        "_sync_voicememory_globals")
    _assert_sync_call(dm_grill.__file__, list(
        n for n in dir(dm_grill.DeathmatchGrillingFlowMixin) if not n.startswith("__")),
        "_sync_deathmatchgrillingflowmixin_globals")
    _assert_sync_call(dm_plan.__file__, list(
        n for n in dir(dm_plan.DeathmatchPlanCoreMixin) if not n.startswith("__")),
        "_sync_deathmatchplancoremixin_globals")
    _assert_sync_call(vfiller.__file__, list(
        n for n in dir(vfiller.VoiceFillerMixin) if not n.startswith("__")),
        "_sync_voicefillermixin_globals")
    _assert_sync_call(vws.__file__, list(
        n for n in dir(vws.VoiceWSMixin) if not n.startswith("__")),
        "_sync_voicewsmixin_globals")


def test_chat_stream_route_delegates(monkeypatch):
    """Executes the route shell: it must delegate to the transport with the
    exact (request, req, db, current_user) arguments."""
    import asyncio
    from types import SimpleNamespace

    import app.api.chat as chat
    import app.api.chat_stream_transport as transport

    sentinel = object()
    seen = {}

    async def fake(request, req, db, current_user):
        seen.update(request=request, req=req, db=db, user=current_user)
        return sentinel

    monkeypatch.setattr(transport, "stream_chat_response", fake)
    request, req = SimpleNamespace(), SimpleNamespace()
    db, user = SimpleNamespace(), SimpleNamespace()
    assert asyncio.run(chat.chat_stream(request, req, db, user)) is sentinel
    assert seen == {"request": request, "req": req, "db": db, "user": user}


def test_transport_drain_gate_executes(monkeypatch):
    """Executes the moved transport body's first branch (drain 503) without
    touching the DB; proves the module is importable and wired at runtime."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    import app.api.chat as chat
    import app.api.chat_stream_transport as transport

    class _Draining:
        is_draining = True

    monkeypatch.setattr(chat, "_agent_registry", _Draining())
    request = SimpleNamespace(conversation_id=None, assistant_id=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(transport.stream_chat_response(
            request, SimpleNamespace(), SimpleNamespace(), SimpleNamespace()))
    assert exc.value.status_code == 503
