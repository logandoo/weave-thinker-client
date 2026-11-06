"""Guard the C-02c kw-bridge codegen contract.

The refactor rewrites a function body into a ``_LoopResult`` bridge.  A codegen
slip produced ``_ = _loop_result._`` (an attribute that cannot exist), which
only fails when a tool block actually executes.  This static check rejects:

* any ``_loop_result.<attr>`` read of a throwaway ``_``;
* any read of an attribute that is neither a dataclass field nor assigned to
  ``_loop_result`` somewhere in the module.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MODULE = Path(__file__).resolve().parents[1] / "app/services/agent_loop_orchestrator.py"


def _bridge_reads_and_writes():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    reads: set[str] = set()
    writes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id == "_loop_result"):
            continue
        if isinstance(node.ctx, ast.Store):
            writes.add(node.attr)
        else:
            reads.add(node.attr)
    return reads, writes


def test_bridge_never_reads_a_throwaway_attribute():
    reads, _ = _bridge_reads_and_writes()
    assert "_" not in reads, "codegen artifact: `_ = _loop_result._`"


def test_bridge_reads_have_dataclass_defaults():
    """First use must be safe: a read cannot rely on a later write.

    The C-02c bridge carries locals across helpers.  If a helper reads an
    attribute that only the caller's `finally` writes afterwards, the first
    tool block raises AttributeError (production conv d6ba1860).  Every read
    must therefore be a declared field with a default.
    """
    from app.services.agent_loop_orchestrator import _LoopResult

    declared = set(_LoopResult.__dataclass_fields__)
    reads, _writes = _bridge_reads_and_writes()
    unknown = reads - declared
    assert not unknown, f"bridge reads without dataclass defaults: {sorted(unknown)}"
