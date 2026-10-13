"""T1 foundation — durable execution 波（P1+P2+D-轻+D-重）纯逻辑地基。

测试面（test seam per docs/PLAN_durable_execution_wave.md T1）：
- can_transition：job 状态机合法迁移白名单（终态不可逆、unknown 仅 reconcile 可出）
- make_key：副作用台账幂等键格式 `{principal_type}:{principal_id}:{cursor}:{tool_name}`
- clamp_concurrency：P2/durable jobs 并发钳制 [1,8]
- CHECKPOINT_VERSION：checkpoint 版本常量
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def test_can_transition_allows_lifecycle():
    from app.services.durable_types import can_transition

    assert can_transition("queued", "leased") is True
    assert can_transition("queued", "cancelled") is True
    assert can_transition("leased", "running") is True
    assert can_transition("running", "succeeded") is True
    assert can_transition("running", "failed") is True
    assert can_transition("running", "cancelled") is True
    assert can_transition("running", "unknown") is True


def test_can_transition_terminal_is_immutable():
    from app.services.durable_types import can_transition

    for terminal in ("succeeded", "failed", "cancelled"):
        for target in ("queued", "leased", "running", "succeeded", "failed", "cancelled", "unknown"):
            assert can_transition(terminal, target) is False, f"{terminal}->{target} must be illegal"


def test_can_transition_unknown_only_settles_to_failed():
    from app.services.durable_types import can_transition

    assert can_transition("unknown", "failed") is True
    assert can_transition("unknown", "succeeded") is False
    assert can_transition("unknown", "running") is False
    assert can_transition("unknown", "queued") is False


def test_can_transition_rejects_unknown_states():
    from app.services.durable_types import can_transition

    assert can_transition("queued", "bogus") is False
    assert can_transition("bogus", "queued") is False


def test_make_key_format():
    from app.services.durable_types import make_key

    assert (make_key("agent_task", "abc", 3, "workspace_write", "c1")
            == "agent_task:abc:3:workspace_write:c1")
    assert (make_key("deathmatch", "conv1", 0, "notes", "c2")
            == "deathmatch:conv1:0:notes:c2")


def test_clamp_concurrency_bounds():
    from app.services.durable_types import clamp_concurrency

    assert clamp_concurrency(0) == 1
    assert clamp_concurrency(-5) == 1
    assert clamp_concurrency(99) == 8
    assert clamp_concurrency(5) == 5
    assert clamp_concurrency(8) == 8
    assert clamp_concurrency("3") == 3


def test_checkpoint_version_constant():
    from app.services.durable_types import CHECKPOINT_VERSION

    assert CHECKPOINT_VERSION == 1
