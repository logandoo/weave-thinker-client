"""批次 A（P0 快修）回归测试 — 2026-09-14。

覆盖：A1 集群嵌入维度源（N1）/ A2 复活衰减锚（N2+DC4）/ A3 注入不截断（F1）
/ A4a 台账计费隔离（DC1）/ A4c stage0 可选硬上限 / A5 压缩工具对边界。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_batch_a_fixes.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.services.memory_cluster_service as mcs
import app.services.memory_weight_service as mws
import app.services.memory_cost_governance_service as cg
import app.services.memory_retrieval_service as mrs
import app.services.agent_service as agent_service
from app.services.context_compressor import ContextCompressor


class _Res:
    def __init__(self, rows=None, scalar=None, rowcount=1):
        self._rows = rows or []
        self._scalar = scalar
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


# ── A1 集群嵌入维度源 ──────────────────────────────────────────────

class _ClusterDB:
    def __init__(self, cluster, rows):
        self._cluster = cluster
        self._rows = rows

    async def get(self, model, cid):
        return self._cluster

    async def execute(self, stmt, params=None):
        return _Res(rows=self._rows)


def test_cluster_embedding_uses_endpoint_dim(monkeypatch):
    """A1：维度取端点（1024）→ 均值写入 + 溯源模型。"""
    cluster = SimpleNamespace(embedding=None, embedding_model=None, member_count=0)
    db = _ClusterDB(cluster, [(object(),)])
    monkeypatch.setattr(mcs, "_emb_from_db", lambda v: [0.25] * 1024)
    monkeypatch.setattr(mcs, "_get_embedding_dim", lambda: 1024)
    monkeypatch.setattr(mcs, "_get_embedding_model", lambda: "test-embed")

    asyncio.run(mcs._update_cluster_embedding(db, "c1"))

    assert cluster.embedding is not None
    assert len(cluster.embedding) == 1024
    assert cluster.member_count == 1
    assert cluster.embedding_model == "test-embed"


def test_cluster_embedding_old_dim_would_mismatch(monkeypatch):
    """负例（N1 机制复现）：维度源回退 1536 时 1024 向量不匹配 → 不写。"""
    cluster = SimpleNamespace(embedding=None, embedding_model=None, member_count=0)
    db = _ClusterDB(cluster, [(object(),)])
    monkeypatch.setattr(mcs, "_emb_from_db", lambda v: [0.25] * 1024)
    monkeypatch.setattr(mcs, "_get_embedding_dim", lambda: 1536)

    asyncio.run(mcs._update_cluster_embedding(db, "c1"))

    assert cluster.embedding is None
    assert cluster.member_count == 0


# ── A2 复活衰减锚（DC4：不得写 last_recalled_at）────────────────────

class _CaptureDB:
    def __init__(self, rows):
        self.sqls = []
        self._rows = rows
        # ORM 化 try_cold_resurrect（余留清零波 2026-10-02）：get 桩返回带
        # 状态的概念对象，行为级断言核对属性变更而非 SQL 字符串。
        self.concepts = {
            r[0]: SimpleNamespace(
                id=r[0], name=r[1], status=r[3], activation_strength=0.5,
                weight=1.0, hot_forget_count=0, weight_decayed_at=None,
                updated_at=None, last_recalled_at=None,
            )
            for r in rows
        }

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return _Res(rows=self._rows)

    async def get(self, model, ident):
        return self.concepts.get(ident)


async def _noop_regen(db, cid):
    return None


def test_resurrect_refreshes_decay_anchor_not_recall(monkeypatch):
    rows = [("c1", "咖啡", None, "cold_forgotten"), ("c2", "美式", None, "silent")]
    db = _CaptureDB(rows)
    monkeypatch.setattr(mws, "_regenerate_embedding_if_stale", _noop_regen)

    out = asyncio.run(mws.try_cold_resurrect(db, "我喜欢咖啡和美式", "u1"))

    assert set(out) == {"c1", "c2"}
    # ORM 化后行为级断言（原 SQL 字符串断言随实现退役，test-change 已声明）：
    # 衰减锚 weight_decayed_at 必须刷新；last_recalled_at 必须不被污染（DC4）。
    c1, c2 = db.concepts["c1"], db.concepts["c2"]
    assert c1.status == "active" and c1.weight_decayed_at is not None
    assert c1.weight == float(mws.config.memory_concept.get("cold_resurrect_weight", 0.3))
    assert c1.hot_forget_count == 0  # 复审 B² Minor-3：A2 契约补全（cold 分支计数清零）
    assert c2.status == "active" and c2.weight_decayed_at is not None
    assert all(db.concepts[k].last_recalled_at is None for k in db.concepts)


# ── A3 记忆注入不截断 ─────────────────────────────────────────────

def test_memory_injection_sections_not_truncated():
    ctx = SimpleNamespace(memory_summary="记" * 1900, dream_summary="", memory_entries=[])
    sections = agent_service._memory_injection_sections(ctx)

    assert len(sections) == 1
    assert len(sections[0]) > 1500
    assert sections[0].endswith("记" * 10)


def test_memory_injection_sections_entries_still_capped():
    entry = SimpleNamespace(title="t", source_type="daily-summary", content="c" * 400)
    ctx = SimpleNamespace(memory_summary="", dream_summary="", memory_entries=[entry])
    sections = agent_service._memory_injection_sections(ctx)

    assert len(sections) == 1
    assert "c" * 250 + "..." in sections[0]


# ── A4a 台账计费隔离（DC1）────────────────────────────────────────

class _AddDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass


def test_record_llm_call_billing_class(monkeypatch):
    # A4.9 Minor：显式固定治理开关，避免环境配置使断言静默失效
    monkeypatch.setattr(cg, "config", SimpleNamespace(memory={"cost_governance_enabled": True}))
    db = _AddDB()
    asyncio.run(cg.record_llm_call(db, "u1", "query_expand", billing_class="read"))
    asyncio.run(cg.record_llm_call(db, "u1", "dream"))

    assert db.added[0].billing_class == "read"
    assert db.added[1].billing_class == "write"


class _GovDB:
    def __init__(self):
        self.sqls = []

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.sqls.append(s)
        if "COUNT" in s:
            return _Res(scalar=3)
        return _Res(scalar=None)


def test_governance_counts_only_billable(monkeypatch):
    db = _GovDB()
    asyncio.run(cg.check_user_threshold_and_degrade(db, "u1"))

    count_sqls = [s for s in db.sqls if "COUNT" in s]
    assert len(count_sqls) == 2
    assert all("billing_class" in s and "'write'" in s for s in count_sqls)


# ── A4c stage0 可选硬上限（默认关）─────────────────────────────────

def test_stage0_ceiling_default_off_and_on(monkeypatch):
    async def slow_norm(text, user_id=None, db=None):
        await asyncio.sleep(0.15)
        return (["k"], None, "semantic", False)

    monkeypatch.setattr(mrs, "_llm_time_normalization", slow_norm)
    fake_cfg = SimpleNamespace(
        memory={"stage0_llm_enabled": False, "stage0_time_regex_patterns": None},
        memory_retrieval={"stage0_hard_ceiling_ms": 50},
    )
    monkeypatch.setattr(mrs, "config", fake_cfg)

    timed_out = asyncio.run(mrs._stage0_query_expansion("x" * 25))
    assert timed_out.keywords != ["k"]  # 超时 → 既有异常回退（jieba）

    fake_cfg.memory_retrieval["stage0_hard_ceiling_ms"] = 0
    full = asyncio.run(mrs._stage0_query_expansion("x" * 25))
    assert full.keywords == ["k"]  # 默认关：行为与旧实现一致


# ── A5 压缩工具对边界 ─────────────────────────────────────────────

def test_tail_cut_never_starts_with_tool():
    comp = ContextCompressor(protect_first_n=0, protect_last_tokens=1, quiet=True)
    messages = [
        {"role": "assistant", "tool_calls": [{"id": "t1", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]

    cut = comp._find_tail_cut_by_tokens(messages, head_end=0)

    assert cut >= len(messages) or messages[cut].get("role") != "tool"


def test_sanitize_tool_pairs_counts_repairs():
    comp = ContextCompressor(quiet=True)
    messages = [
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
    ]

    out = comp._sanitize_tool_pairs(messages)

    assert comp.tool_pair_repairs == 1
    assert all(m.get("role") != "tool" for m in out)
