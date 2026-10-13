"""W12 Dx-1：记忆身份归属错乱修复（60 生产：蛮蛮=弟弟/年年=姐姐/用户=妈妈 被记反；
罗航=系统自身 被记成用户）。

钉住的修复点（对应 08-24 诊断遗留根因 1/3/4/6 + 罗航消歧）：
- F1  consolidation `_rebalance_clusters`：`profile_fact:` 出处计入存活证据，
  不再把画像概念当零证据误杀（60 生产 2026-10-08 00:03 仍在杀正确行）。
- F1b `reconcile_concept_sources`：同款出处不计入 → 不再误标 needs_review。
- F2  `sync_profile_concepts` 重建命中已失效（valid_to 置位）同名行 → 复活
  （valid_to=NULL + needs_review=FALSE），merged 行不阻塞重建。
- F3  提取提示词：主体归属 + 关系方向性约束（profile 提炼 + 潜意识概念提取）。
- F4  系统提示词身份节：罗航=助手/系统自身 ≠ 用户 消歧 + 复述用户经历保持视角。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_identity_attribution.py -v
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import app.services.memory_consolidation_service as cons
import app.services.memory_concept_service as concept_svc
import app.services.memory_profile_service as profile_svc
from app.services.agent_service import AgentService
from app.services.memory_service import AgentSharedContext


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _ProfileKilledDb:
    """profile 概念（raw_ids 仅 profile_fact:，无 unit_ids）已被打 needs_review 的场景。"""

    def __init__(self, raw_ids_json: str, unit_ids_json: str = "[]"):
        self.review_row = ("c-prof", raw_ids_json, unit_ids_json)
        self.killed: list[str] = []
        self.cleared: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "DELETE FROM memory_clusters" in sql:
            return _FakeResult()
        if "needs_review = TRUE" in sql and "FROM memory_concepts" in sql:
            return _FakeResult(rows=[self.review_row])
        if "SELECT COUNT(*) FROM subconscious_log" in sql:
            return _FakeResult(scalar=0)
        if "SET valid_to = NOW()" in sql:
            self.killed.append(params.get("id"))
            return _FakeResult()
        if "SET needs_review = FALSE" in sql:
            self.cleared.append(params.get("id"))
            return _FakeResult()
        if "FROM memory_clusters" in sql:
            return _FakeResult(rows=[])  # 无簇，跳过 rebalance 尾段
        return _FakeResult()

    async def commit(self):
        pass


def _run(coro):
    return asyncio.run(coro)


# ── F1：consolidation 不再误杀 profile_fact: 出处概念 ──

def test_rebalance_does_not_kill_profile_fact_concepts():
    db = _ProfileKilledDb(json.dumps(["profile_fact:用户是年年的妈妈"], ensure_ascii=False))
    _run(cons._rebalance_clusters(db, "u1", {}))
    assert db.killed == [], "profile_fact: 出处被当零证据误杀（valid_to 置位）"
    assert db.cleared == ["c-prof"], "有存活出处应清除 needs_review 标记"


def test_rebalance_still_kills_truly_sourceless_concepts():
    # 对照腿：出处确实是已消失的 messages 行（raw_alive 查询不到）→ 仍应杀死
    class _DeadRawDb(_ProfileKilledDb):
        async def execute(self, stmt, params=None):
            # consolidation 的 raw_alive 不查库（仅 legacy:/profile_fact: 直接计），
            # 普通 raw id 不会命中前缀 → raw_alive=0 → 死亡路径保持
            return await super().execute(stmt, params)

    db = _DeadRawDb(json.dumps(["dead-message-id-xyz"], ensure_ascii=False))
    _run(cons._rebalance_clusters(db, "u1", {}))
    assert db.killed == ["c-prof"], "零证据概念仍应被置 valid_to（守卫语义不变）"


# ── F1b：reconcile 不再误标 profile_fact: 出处概念 ──

class _ReconcileDb:
    def __init__(self, raw_ids_json: str):
        self.row = ("c-prof", raw_ids_json, "[]")
        self.flagged: list[str] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "FROM memory_concepts" in sql and "needs_review = FALSE" in sql:
            return _FakeResult(rows=[self.row])
        if "SELECT COUNT(*) FROM subconscious_log" in sql:
            return _FakeResult(scalar=0)
        if "SET needs_review = TRUE" in sql:
            self.flagged.append(params.get("id"))
            return _FakeResult()
        if "SELECT (" in sql and "FROM messages" in sql:
            return _FakeResult(scalar=0)  # 普通 raw id 查无此行
        return _FakeResult()


def test_reconcile_does_not_flag_profile_fact_concepts():
    db = _ReconcileDb(json.dumps(["profile_fact:用户是年年的妈妈"], ensure_ascii=False))
    flagged = _run(concept_svc.reconcile_concept_sources(db, "u1"))
    assert flagged == 0 and db.flagged == [], "profile_fact: 出处不应被误标 needs_review"


def test_reconcile_still_flags_dead_raw_ids():
    db = _ReconcileDb(json.dumps(["dead-message-id-xyz"], ensure_ascii=False))
    flagged = _run(concept_svc.reconcile_concept_sources(db, "u1"))
    assert flagged == 1 and db.flagged == ["c-prof"], "死出处概念仍应标 needs_review（守卫语义不变）"


# ── F2：sync_profile_concepts 重建复活已失效同名行 ──

class _SyncDb:
    def __init__(self):
        self.updates: list[tuple[str, dict]] = []
        self.inserts = 0

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "SELECT metadata_json FROM user_agent_states" in sql:
            return _FakeResult(scalar=None)  # cost governance：无降级
        if "FROM memory_episodes" in sql:
            return _FakeResult(rows=[("用户说：我是年年的妈妈，蛮蛮是弟弟，2024年11月出生。",)])
        if "source_raw_ids FROM memory_concepts" in sql and "valid_to IS NULL" in sql:
            return _FakeResult(rows=[])  # 活体键集为空（旧行已失效不阻塞）
        if "canonical_name" in sql and "FROM memory_concepts" in sql and "SELECT" in sql:
            return _FakeResult(rows=[("c-killed", None)])  # 命中已失效同名行（无既有 raw_ids）
        if "UPDATE MEMORY_CONCEPTS" in sql.upper():
            self.updates.append((sql, params))
            return _FakeResult()
        if "UPDATE user_agent_states" in sql:
            return _FakeResult()
        return _FakeResult()

    def begin_nested(self):
        raise RuntimeError("fake: no nested tx")  # record_llm_call 有 try 兜底

    async def commit(self):
        pass


def test_sync_profile_resurrects_invalidated_row(monkeypatch):
    monkeypatch.setattr(
        profile_svc, "_extract_profile_facts",
        lambda llm, summaries: _async_ret([{"name": "用户是年年的妈妈", "desc": "用户是女儿年年的妈妈"}]),
    )
    # 重 embed 出网调用打桩
    import app.services.memory_concept_service as cs
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(cs, "update_concept_description", _noop)

    db = _SyncDb()
    out = _run(profile_svc.sync_profile_concepts(db, "u1", force=True))
    assert out.get("updated") == 1, f"应走更新复活分支: {out}"
    resurrect = [s for s, _ in db.updates if "valid_to = NULL" in s.replace("  ", " ") or "valid_to=NULL" in s]
    assert resurrect, "更新分支必须复活 valid_to=NULL（08-24 根因 6：僵尸态无复活路径）"


async def _async_ret(v):
    return v


# ── F3：提取提示词主体归属/方向性 ──

class _CaptureLlm:
    def __init__(self):
        self.messages = None

    async def complete_chat(self, messages, **kwargs):
        self.messages = messages
        return "[]"


def test_profile_extraction_prompt_enforces_attribution_and_directionality():
    llm = _CaptureLlm()
    _run(profile_svc._extract_profile_facts(llm, ["用户说：我是妈妈，蛮蛮是弟弟，年年是姐姐。"]))
    prompt = llm.messages[0]["content"]
    assert "方向" in prompt and "弟弟" in prompt and "姐姐" in prompt, "提示词缺关系方向性约束"
    assert "妈妈" in prompt, "提示词缺用户自身角色保真约束"
    assert "罗航" in prompt and ("不是用户" in prompt or "≠ 用户" in prompt or "≠用户" in prompt), \
        "提示词缺产品名≠用户名的主体归属约束"


def test_subconscious_extraction_prompt_enforces_attribution(monkeypatch):
    captured = {}

    class _Llm:
        async def complete_chat(self, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return '{"episodic": null, "concepts": []}'

    import app.services.memory_llm_factory as factory
    monkeypatch.setattr(factory, "_memory_llm", lambda purpose: _Llm())
    import app.services.memory_subconscious_service as sub
    async def _empty_concepts(db, uid):
        return []
    async def _empty_clusters(db, uid):
        return []
    monkeypatch.setattr(
        "app.services.memory_concept_service.get_concepts_for_extraction", _empty_concepts)
    monkeypatch.setattr(
        "app.services.memory_concept_service.get_clusters_for_extraction", _empty_clusters)

    class _Db:
        async def execute(self, stmt, params=None):
            return _FakeResult(scalar=99)  # 越过 bootstrap 门控

    _run(sub._call_extraction_llm([{"raw_text": "用户：蛮蛮是弟弟。罗航是系统自己。", "source_trust": "user_stated"}], _Db(), "u1"))
    prompt = captured.get("prompt", "")
    assert prompt, "未捕获提取提示词"
    assert "罗航" in prompt, "概念提取提示词缺产品/助手名主体提示"
    assert "方向" in prompt or "弟弟" in prompt, "概念提取提示词缺关系方向性约束"


# ── F4：系统提示词身份消歧 + 视角保持 ──

def _build_prompt() -> str:
    service = AgentService()
    shared = AgentSharedContext(agent_state=None, memory_summary="", dream_summary="", memory_entries=[])
    workspace = SimpleNamespace(root_path="/tmp/wt-identity-test")
    return asyncio.run(
        service._build_system_prompt(
            assistant=None, shared_context=shared, workspace=workspace, user=None,
            identity_context="", deathmatch_mode=False,
        )
    )


def test_system_prompt_disambiguates_luohang_from_user():
    prompt = _build_prompt()
    assert "罗航" in prompt and "不是" in prompt and "用户" in prompt
    # 必须有显式消歧句（罗航=助手自己 ≠ 用户）
    import re
    assert re.search(r"罗航[^。\n]*(不是|≠)[^。\n]*用户|用户[^。\n]*(不是|≠)[^。\n]*罗航", prompt), \
        "身份节缺「罗航≠用户」显式消歧"


def test_system_prompt_enforces_user_perspective_on_retelling():
    prompt = _build_prompt()
    import re
    assert re.search(r"复述|确认|引用", prompt) and re.search(r"视角|第一人称|人称", prompt), \
        "身份节缺复述用户经历时保持视角的规则（08-24 你/我翻转根因）"


# ── 修复轮（A4.9 复审 findings）──

def test_answer_system_disambiguates_luohang():
    """复审 B-I1：direct_reply 的 answer_system 同样要有 罗航≠用户 + 视角条款
    （身份问题恰恰走这条路）。"""
    src = open("app/services/agent_loop.py", encoding="utf-8").read()
    idx = src.find("async def _stream_direct_reply")
    assert idx > -1
    block = src[idx:idx + 6000]
    assert "不是用户的名字" in block and "罗航" in block, \
        "direct_reply answer_system 缺罗航≠用户消歧"
    assert "视角" in block, "direct_reply answer_system 缺视角保持条款"


def test_sync_update_branch_merges_profile_fact_key(monkeypatch):
    """复审 A-M4：已有（死）raw_ids 的画像行在更新复活时必须补入 profile_fact: 键，
    否则 reconcile 每晚再标、rebalance 再杀、sync 再复活—— nightly flap。"""
    monkeypatch.setattr(
        profile_svc, "_extract_profile_facts",
        lambda llm, summaries: _async_ret([{"name": "用户是年年的妈妈", "desc": "用户是女儿年年的妈妈"}]),
    )
    import app.services.memory_concept_service as cs
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(cs, "update_concept_description", _noop)

    class _FlapDb(_SyncDb):
        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "canonical_name" in sql and "FROM memory_concepts" in sql and "SELECT" in sql:
                # 命中行：携带既有死 raw_ids（迁移期残留）
                return _FakeResult(rows=[("c-killed", '["dead-old-id"]')])
            return await super().execute(stmt, params)

    db = _FlapDb()
    out = _run(profile_svc.sync_profile_concepts(db, "u1", force=True))
    assert out.get("updated") == 1, f"应走更新复活分支: {out}"
    key_updates = [p for s, p in db.updates if "SOURCE_RAW_IDS" in " ".join(s.split()).upper()]
    assert key_updates, "更新分支必须回写 source_raw_ids"
    merged = json.loads(key_updates[-1]["keys"])
    assert "profile_fact:用户是年年的妈妈" in merged and "dead-old-id" in merged, \
        f"profile_fact 键须 append 合并而非仅空时补写: {merged}"
