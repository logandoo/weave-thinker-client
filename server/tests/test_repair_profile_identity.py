"""W12 Dx-1：repair_profile_identity 修复器守卫测试（假 DB，离线）。

钉住：
- list 仅读（dry-run 语义）；
- resurrect 守卫：仅 profile+active 可复活；merged/非 profile/缺失不复活；
  已活体无新描述 → skipped:already-live；
- supersede 改写描述并复活（SQL 含 valid_to=NULL + description 更新）；
- anchor：命中已失效同名 active 行 → resurrected；未命中 → create_concept 新建
  并回写 profile_fact: 键。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_repair_profile_identity.py -v
"""
import asyncio
import json

import scripts.repair_profile_identity as rep


class _R:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _run(coro):
    return asyncio.run(coro)


class _Db:
    """raw_ids/行形态按调用路由；记录全部 UPDATE。"""

    def __init__(self, profile_row=None):
        # profile_row: (id, status, valid_to) | None（resurrect 目标查询）
        self.profile_row = profile_row
        self.updates: list[tuple[str, dict]] = []
        self.select_desc_rows = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        u = " ".join(sql.split()).upper()
        if u.startswith("SELECT ID, STATUS, VALID_TO FROM MEMORY_CONCEPTS"):
            return _R([self.profile_row] if self.profile_row else [])
        if "FROM MEMORY_CONCEPTS" in u and "CANONICAL_NAME" in u:
            return _R(self.select_desc_rows)
        if u.startswith("UPDATE MEMORY_CONCEPTS"):
            self.updates.append((sql, params or {}))
            return _R()
        return _R()


def test_resurrect_killed_active_profile_row():
    db = _Db(profile_row=("c1", "active", "2026-10-08 00:03:44"))
    out = _run(rep.resurrect_concept(db, "u1", "c1"))
    assert out == "applied"
    assert any("VALID_TO = NULL" in " ".join(s.split()).upper() and "NEEDS_REVIEW = FALSE" in " ".join(s.split()).upper()
               for s, _ in db.updates)


def test_resurrect_refuses_merged_row():
    db = _Db(profile_row=("c1", "merged", "2026-10-08 00:03:44"))
    out = _run(rep.resurrect_concept(db, "u1", "c1"))
    assert out == "skipped:status=merged"
    assert db.updates == [], "merged 行不得复活（避免与合并目标重复入注入面）"


def test_resurrect_refuses_missing_or_non_profile():
    db = _Db(profile_row=None)
    out = _run(rep.resurrect_concept(db, "u1", "c404"))
    assert out == "skipped:not-profile-or-missing"
    assert db.updates == []


def test_resurrect_live_row_without_desc_skips():
    db = _Db(profile_row=("c1", "active", None))
    out = _run(rep.resurrect_concept(db, "u1", "c1"))
    assert out == "skipped:already-live"
    assert db.updates == []


def test_supersede_rewrites_description_and_resurrects(monkeypatch):
    async def _noop(*a, **k):
        return True
    monkeypatch.setattr("app.services.memory_concept_service.update_concept_description", _noop)
    db = _Db(profile_row=("c1", "active", "2026-09-20 00:00:57"))
    out = _run(rep.resurrect_concept(db, "u1", "c1", "用户的姓名不是罗航；罗航是 AI 助手自身。"))
    assert out == "applied"
    joined = " ".join(" ".join(s.split()).upper() for s, _ in db.updates)
    assert "VALID_TO = NULL" in joined and "DESCRIPTION_SHORT = :D" in joined
    assert db.updates[0][1]["d"].startswith("用户的姓名不是罗航")


def test_anchor_resurrects_existing_killed_namesake(monkeypatch):
    async def _noop(*a, **k):
        return True
    monkeypatch.setattr("app.services.memory_concept_service.update_concept_description", _noop)
    db = _Db()
    db.select_desc_rows = [("c-old", "2026-09-20 00:00:57")]  # (id, valid_to) 已失效同名
    out = _run(rep.upsert_anchor(db, "u1", "用户身份边界", "罗航是 AI 助手自身，不是用户。"))
    assert out == "resurrected"
    joined = " ".join(" ".join(s.split()).upper() for s, _ in db.updates)
    assert "VALID_TO = NULL" in joined


def test_anchor_creates_when_absent(monkeypatch):
    created = {}

    async def _create(db, uid, name, short, full, **kw):
        created.update(name=name, kw=kw)
        return "c-new"
    async def _noop(*a, **k):
        return True
    monkeypatch.setattr("app.services.memory_concept_service.create_concept", _create)
    monkeypatch.setattr("app.services.memory_concept_service.update_concept_description", _noop)
    db = _Db()
    db.select_desc_rows = []
    out = _run(rep.upsert_anchor(db, "u1", "家庭成员关系", "蛮蛮是弟弟，年年是姐姐；用户是妈妈。"))
    assert out == "created"
    assert created["name"] == "家庭成员关系"
    assert created["kw"].get("memory_type") == "profile"
    assert created["kw"].get("source_trust") == "user_stated"
    key_updates = [p for s, p in db.updates if "SOURCE_RAW_IDS" in " ".join(s.split()).upper()]
    assert key_updates and json.loads(key_updates[0]["k"]) == ["profile_fact:家庭成员关系"]


# ── 修复轮（A4.9 复审 C1）──

def test_profile_source_type_matches_profile_sync():
    """复审 C1：画像行的 source_type 恒为 'profile_sync'（_PROFILE_SOURCE）。
    脚本若写成 'profile' 会在 60 全空转 + anchor 建重复行。"""
    import re
    from app.services.memory_profile_service import _PROFILE_SOURCE
    assert _PROFILE_SOURCE == "profile_sync"
    import inspect
    src = inspect.getsource(rep)
    # 裸 'profile' 只允许出现在 memory_type 位；source_type 不得使用
    assert not re.search(r"source_type\s*=\s*['\"]profile['\"]", src), \
        "脚本残留 source_type='profile' 字面量（应为 'profile_sync'）"
    assert not re.search(r"source_type = 'profile'(?!_sync)", src), \
        "SQL 残留 source_type = 'profile'（应为 profile_sync）"


def test_anchor_create_uses_profile_sync_source_type(monkeypatch):
    created = {}

    async def _create(db, uid, name, short, full, **kw):
        created.update(kw=kw)
        return "c-new"
    monkeypatch.setattr("app.services.memory_concept_service.create_concept", _create)
    db = _Db()
    db.select_desc_rows = []
    out = _run(rep.upsert_anchor(db, "u1", "家庭成员关系", "蛮蛮是弟弟，年年是姐姐；用户是妈妈。"))
    assert out == "created"
    assert created["kw"].get("source_type") == "profile_sync"


def test_cli_execute_flag_after_subcommand(monkeypatch):
    """生产实踩：argparse 主 parser 的 --execute 在子命令后不被识别（unrecognized），
    三条修复命令在 60 全部静默失败。子命令须各自携带 --execute。"""
    captured = {}

    async def _fake_amain(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(rep, "_amain", _fake_amain)
    monkeypatch.setattr("sys.argv", ["x", "--user", "YearDoo", "resurrect", "abc-123", "--execute"])
    assert rep.main() == 0
    assert captured["args"].execute is True and captured["args"].action == "resurrect"
    assert captured["args"].concept_id == "abc-123"

    monkeypatch.setattr("sys.argv", ["x", "--user", "u", "anchor", "--name", "n", "--desc", "d", "--execute"])
    assert rep.main() == 0
    assert captured["args"].execute is True and captured["args"].name == "n"

    # dry-run 默认
    monkeypatch.setattr("sys.argv", ["x", "--user", "u", "anchor", "--name", "n", "--desc", "d"])
    assert rep.main() == 0
    assert captured["args"].execute is False
