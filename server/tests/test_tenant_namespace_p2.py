"""P2 multi-tenant: tenant id creates a filesystem namespace, default unchanged."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tenant_service import scoped_root, tenant_segment


def test_scoped_root_no_tenant_is_legacy():
    assert scoped_root(Path("/data/ws"), tenant="") == Path("/data/ws")


def test_scoped_root_with_tenant():
    assert scoped_root(Path("/data/ws"), tenant="acme") == Path("/data/ws/acme")


def test_tenant_segment_sanitizes_env(monkeypatch):
    # A rewritten id gets a stable hash suffix so distinct configured ids do
    # not collide after lossy sanitisation.
    monkeypatch.setenv("WT_TENANT_ID", "Acme / Prod")
    seg = tenant_segment()
    assert seg.startswith("Acme-Prod-")
    assert tenant_segment() == seg
    monkeypatch.setenv("WT_TENANT_ID", "Acme-Prod")
    assert tenant_segment() != seg


def test_system_changelog_is_not_tenant_scoped(tmp_path, monkeypatch):
    from app.tools.memory import _get_memory_path
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("WT_TENANT_ID", "acme")
    monkeypatch.setenv("WT_MULTI_TENANT_EXPERIMENTAL", "true")
    assert _get_memory_path("system", "u1", ensure_dir=False) == tmp_path / "changelog.md"
    assert _get_memory_path("user", "u1", ensure_dir=False) == tmp_path / "acme" / "u1" / "USER.md"


def test_tenant_without_experimental_flag_is_refused(tmp_path, monkeypatch):
    from app.services.tenant_service import TenantConfigError, tenant_config_errors
    from app.tools.memory import _get_memory_dir

    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("WT_TENANT_ID", "acme")
    monkeypatch.delenv("WT_MULTI_TENANT_EXPERIMENTAL", raising=False)
    assert tenant_config_errors()
    try:
        _get_memory_dir()
    except TenantConfigError:
        pass
    else:
        raise AssertionError("tenant mode without multi_tenant_experimental must be refused")


def test_terminal_accepts_persisted_workspace_and_blocks_relative_traversal(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from app.tools import terminal

    monkeypatch.setattr(terminal, "config", SimpleNamespace(
        workspace_root=tmp_path,
        super_admin_bypass=False,
    ))
    legacy = tmp_path / "alice-legacy"
    legacy.mkdir()
    cwd, err = terminal._resolve_working_dir({}, {"workspace_path": str(legacy)})
    assert err is None and cwd == str(legacy.resolve())
    _cwd, err2 = terminal._resolve_working_dir({}, {"workspace_path": "/etc"})
    assert err2 and "outside the allowed workspace" in err2
    assert terminal._command_accesses_outside_workspace(
        "cat ../bob/secret.txt", str(legacy), str(legacy),
    ) == "../bob/secret.txt"
    assert terminal._command_accesses_outside_workspace(
        "cd .. && ls", str(legacy), str(legacy),
    ) == ".."
    (legacy / "sub").mkdir()
    assert terminal._command_accesses_outside_workspace(
        "cd .. && ls", str(legacy / "sub"), str(legacy),
    ) is None
