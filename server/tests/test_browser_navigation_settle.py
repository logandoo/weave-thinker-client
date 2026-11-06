"""Regression: browser_navigate must not return on the SPA boot/default state.

Live evidence (conv 7c9bad25, 2026-11-03): ComfyUI creates ``<canvas>`` at
~5.3 s, its boot/default graph stays stable for ~2 s, and only at ~7.3 s does
the site replace it with the 9-node ``#qwen21-t2i-flat`` workflow.  The old
``navigate()`` returned immediately after ``wait_for_selector("canvas")``; the
agent then read the default graph, wrote to node ids 67/71/68 and submitted the
wrong workflow.

Note the fake page deliberately models Playwright's *one-shot* networkidle
semantics: ``wait_for_load_state("networkidle")`` never advances the app state.
The fix must therefore rely on real state observation (or wait_js), not on a
second networkidle call.

Run from backend/:  python -m pytest tests/test_browser_navigation_settle.py -v
"""
import asyncio
import json

import pytest

import app.services.interactive_browser_service as ibs
from app.services.interactive_browser_service import BrowserSession


class _FakePage:
    def __init__(self, *, selector_found=True, wait_js_error=None, signature_stage_at=4,
                 has_large_canvas=True):
        self.stage = 0                 # 0 boot, 1 canvas/default graph, 2 target
        self.url = "http://example.test/"
        self.calls = []
        self.selector_found = selector_found
        self.wait_js_error = wait_js_error       # None | "timeout" | "reference"
        self.signature_stage_at = signature_stage_at
        self.has_large_canvas = has_large_canvas
        self.signature_calls = 0

    async def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(("goto", url, wait_until))
        self.url = url
        self.stage = 0

    async def wait_for_load_state(self, state, timeout=None):
        # One-shot lifecycle event: deliberately does NOT advance the app state.
        self.calls.append(("load_state", state, self.stage))

    async def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("selector", selector, self.stage))
        if not self.selector_found:
            raise TimeoutError("selector not found")
        self.stage = max(self.stage, 1)

    async def wait_for_function(self, script, timeout=None):
        self.calls.append(("wait_js", script, self.stage))
        if self.wait_js_error == "timeout":
            raise TimeoutError("wait_js timed out")
        if self.wait_js_error == "reference":
            raise ReferenceError("app is not defined")
        self.stage = 2

    async def evaluate(self, script):
        if "MutationObserver" in script:
            self.calls.append(("evaluate", "observer"))
            return True
        if "querySelectorAll('canvas')" in script and "innerWidth" in script:
            self.calls.append(("evaluate", "has_canvas"))
            return self.has_large_canvas
        self.calls.append(("evaluate", "signature"))
        self.signature_calls += 1
        if self.signature_calls >= self.signature_stage_at:
            self.stage = 2
        self.url = "http://example.test/#target" if self.stage == 2 else "http://example.test/"
        sig = {
            "u": self.url,
            "t": "boot" if self.stage == 0 else ("ComfyUI" if self.stage == 1 else "*Unsaved Workflow - ComfyUI"),
            "m": 0,
            "r": "complete",
            "c": "1280x720@1280x720",
        }
        return json.dumps(sig)

    async def title(self):
        return {0: "boot", 1: "ComfyUI", 2: "*Unsaved Workflow - ComfyUI"}[self.stage]


class _FakeHandle:
    async def close(self):
        return None


def _session(page):
    return BrowserSession(
        session_id="nav-test",
        browser=_FakeHandle(),
        context=_FakeHandle(),
        page=page,
    )


@pytest.fixture(autouse=True)
def _patched_snapshot(monkeypatch):
    async def _snap(session, full=False):
        return f"stage={session.page.stage}"

    monkeypatch.setattr(ibs, "_take_snapshot", _snap)
    # Keep the real settle loop, but shrink its windows so tests stay fast.
    # The canvas minimum observation window is what must carry the test over
    # the boot/default plateau (the fake swaps at signature call 4).
    monkeypatch.setattr(ibs, "_CANVAS_SETTLE_MIN_MS", 700)
    monkeypatch.setattr(ibs, "_CANVAS_SETTLE_QUIET_MS", 50)
    monkeypatch.setattr(ibs, "_DEFAULT_SETTLE_QUIET_MS", 50)
    monkeypatch.setattr(ibs, "_SETTLE_MAX_MS", 1500)


def test_navigate_observes_real_quiescence_not_networkidle():
    page = _FakePage()
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="canvas"))

    assert result["success"] is True
    # The busted behavior returned stage=1 (default graph).  The fixed call
    # must return only after the late workflow swap.
    assert result["snapshot"] == "stage=2"
    assert result["title"] == "*Unsaved Workflow - ComfyUI"
    assert result["settled"] is True
    assert result["settle_mode"] == "canvas_heuristic"
    assert result["wait_selector_found"] is True
    # No post-selector networkidle: the one-shot lifecycle event is not proof.
    assert [c[0] for c in page.calls].count("load_state") == 1
    assert any(c[0] == "evaluate" for c in page.calls)
    # Canvas stability is never claimed as target readiness.
    assert result["warnings"] and "stability is not proof" in result["warnings"][0]


def test_navigate_wait_js_is_authoritative_and_skips_heuristic_settle():
    page = _FakePage()
    session = _session(page)
    predicate = "() => typeof app !== 'undefined' && app.graph?.nodes?.some(n => n.type === 'TextEncodeQwenImage21')"

    result = asyncio.run(
        ibs.navigate(session, "http://example.test/app", wait_selector="canvas", wait_js=predicate)
    )

    assert result["success"] is True
    assert result["snapshot"] == "stage=2"
    assert result["settled"] is True
    assert result["settle_mode"] == "wait_js"
    assert result["canvas"] is True
    assert page.calls[-1][0] == "wait_js"
    assert page.calls[-1][1] == predicate
    assert [c[0] for c in page.calls].count("load_state") == 1
    # wait_js is authoritative: no stability sampling (observer/signature).
    assert not any(c[0] == "evaluate" and c[1] in ("observer", "signature") for c in page.calls)


def test_navigate_wait_js_timeout_fails_closed_and_drops_refs(monkeypatch):
    page = _FakePage(wait_js_error="timeout")
    session = _session(page)

    async def _snap_with_refs(session, full=False):
        session.ref_map = {"__br_9": {"tag": "button"}}
        return "stage=1"

    monkeypatch.setattr(ibs, "_take_snapshot", _snap_with_refs)

    result = asyncio.run(
        ibs.navigate(
            session,
            "http://example.test/app",
            wait_selector="canvas",
            wait_js="() => window.__never_ready__ === true",
        )
    )

    assert result["success"] is False
    assert "not met" in result["error"]
    assert result["settled"] is False
    assert result["wait_selector_found"] is True
    assert result["snapshot"] == "stage=1"
    # Failure must not leave clickable refs on the boot page.
    assert session.ref_map == {}


def test_navigate_wait_js_evaluation_error_is_not_reported_as_timeout():
    page = _FakePage(wait_js_error="reference")
    session = _session(page)

    result = asyncio.run(
        ibs.navigate(
            session,
            "http://example.test/app",
            wait_selector="canvas",
            wait_js="() => app.graph.nodes.length > 0",
        )
    )

    assert result["success"] is False
    assert "evaluation failed" in result["error"]
    assert "guard references" in result["error"]
    assert "not met" not in result["error"]


def test_navigate_reports_missing_selector_instead_of_silent_success():
    page = _FakePage(selector_found=False)
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="canvas"))

    assert result["success"] is True
    assert result["wait_selector_found"] is False
    assert result["warnings"] and "canvas" in result["warnings"][0]


def test_navigate_evaluate_failure_is_not_stability():
    class _FailingEvalPage(_FakePage):
        async def evaluate(self, script):
            if "MutationObserver" in script:
                return True
            if "querySelectorAll('canvas')" in script and "innerWidth" in script:
                return True
            raise RuntimeError("renderer wedged")

    session = _session(_FailingEvalPage())
    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="canvas"))

    assert result["success"] is True
    assert result["settled"] is False
    assert result["warnings"] and "did not reach a stable state" in result["warnings"][0]


def test_navigate_settle_timeout_is_visible_not_swallowed(monkeypatch):
    class _EverChangingPage(_FakePage):
        async def evaluate(self, script):
            if "MutationObserver" in script:
                return True
            self.signature_calls += 1
            return json.dumps({"u": "http://example.test/", "t": "x", "m": self.signature_calls})

    session = _session(_EverChangingPage())
    monkeypatch.setattr(ibs, "_SETTLE_MAX_MS", 350)  # shorter than two poll intervals

    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="canvas"))

    assert result["success"] is True
    assert result["settled"] is False
    assert result["warnings"] and "stable state" in result["warnings"][0]
    # The monkeypatched budget must actually bound the sampler.
    assert result["settle_ms"] < 1000


def test_navigate_detects_large_canvas_without_canvas_selector():
    page = _FakePage(has_large_canvas=True)
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="#app"))

    assert result["success"] is True
    assert result["settle_mode"] == "canvas_heuristic"
    assert ("evaluate", "has_canvas") in page.calls


def test_navigate_without_selector_keeps_old_single_idle_wait():
    page = _FakePage(has_large_canvas=False)
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/plain"))

    assert result["success"] is True
    assert [c[0] for c in page.calls].count("load_state") == 1
    assert "wait_selector_found" not in result
    assert "settled" not in result


def test_navigate_without_selector_still_settles_large_canvas():
    page = _FakePage(has_large_canvas=True)
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/game"))

    assert result["success"] is True
    assert result["canvas"] is True
    assert result["settle_mode"] == "canvas_heuristic"
    assert "settled" in result


def test_static_boot_canvas_is_not_claimed_ready_without_a_transition():
    # The boot/loading canvas never changes during the window: the sampler may
    # report "stable", but it must not imply target readiness.
    page = _FakePage(has_large_canvas=True, signature_stage_at=10**6)
    session = _session(page)

    result = asyncio.run(ibs.navigate(session, "http://example.test/app", wait_selector="canvas"))

    assert result["success"] is True
    assert result["settled"] is True
    assert result["observed_change"] is False
    joined = " ".join(result["warnings"] or [])
    assert "no state transition" in joined
    assert "stability is not proof" in joined


def test_navigate_schema_teaches_target_specific_wait_js():
    from app.tools.browser_interact import _NAVIGATE_SCHEMA

    props = _NAVIGATE_SCHEMA["parameters"]["properties"]
    assert "wait_js" in props
    desc = props["wait_js"]["description"].lower()
    assert "target document" in desc
    # The weak "app exists" example must not be the documented default.
    assert "nodes?.length > 0" not in desc
    top = _NAVIGATE_SCHEMA["description"]
    assert "boot/default" in top
