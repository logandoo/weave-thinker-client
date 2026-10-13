"""retrieve_with_meta — meta 读出接口（ctx + 注入 memory_ids + top 绝对相关分）。

双路径对账：retrieve_and_build_context（文本通道，行为须逐字节不变）必须与
retrieve_with_meta 同参返回同一 ctx。运行:
    cd backend && /home/user/Documents/DEV/chatbot/.venv/bin/python -m pytest tests/test_memory_retrieve_meta.py -v
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.services.memory_retrieval_service as mrs
from app.services.memory_retrieval_service import RetrievalCandidate, Stage0Result


def _mk(id, tier, score, rerank=None):
    meta = {}
    if rerank is not None:
        meta["rerank_score"] = rerank
    return RetrievalCandidate(id=id, tier=tier, content="c-" + id, score=score, metadata=meta)


def _patch_stages(monkeypatch, candidates):
    """把阶段管线桩成固定候选集（db=None 下不触真实 DB/LLM）。"""
    async def fake_stage0(query_text, user_id=None, db=None):
        return Stage0Result(keywords=["kw"], query_type="general", include_expired=False)

    async def fake_cold(db, uid):
        return False

    async def fake_stage1(db, uid, stage0, cold_start):
        return list(candidates)

    async def fake_stage2(db, uid, cc, stage0, qt, param_overrides=None):
        return list(cc)

    async def fake_stage3(db, uid, qt, cc, ec, sc, stage0):
        return list(cc) + list(ec) + list(sc)

    async def fake_stage4(cands, qt, user_id=None, db=None, cold_start=False):
        return list(cands)

    async def fake_inject_ex(db, uid, final, stage0, query_text=""):
        return ("(CTX n=%d)" % len(final),
                {"truncated": False, "budget_chars": 2000, "injected_chars": 10})

    async def fake_boosts(*a, **k):
        return None

    monkeypatch.setattr(mrs, "_stage0_query_expansion", fake_stage0)
    monkeypatch.setattr(mrs, "_is_cold_start", fake_cold)
    monkeypatch.setattr(mrs, "_stage1_bm25_search", fake_stage1)
    monkeypatch.setattr(mrs, "_stage2_description_expansion", fake_stage2)
    monkeypatch.setattr(mrs, "_stage3_embedding_rerank", fake_stage3)
    monkeypatch.setattr(mrs, "_stage4_rerank", fake_stage4)
    monkeypatch.setattr(mrs, "_build_injection_context_ex", fake_inject_ex)
    monkeypatch.setattr(mrs, "_emit_recall_log", lambda *a, **k: None)
    monkeypatch.setattr(mrs, "_apply_recall_boosts_bg", fake_boosts)
    monkeypatch.setattr(mrs, "_session_cache_get", lambda key: None)
    monkeypatch.setattr(mrs, "_session_cache_put", lambda key, value, meta=None, stats=None: None)
    monkeypatch.setattr(mrs, "_record_window_hit", lambda *a, **k: None)


def test_meta_returns_ctx_ids_top(monkeypatch):
    cands = [
        _mk("c1", "concept", 1.0, rerank=0.9),
        _mk("c2", "concept", 0.8, rerank=0.7),
        _mk("e1", "episodic", 0.6, rerank=0.5),
        _mk("s1", "subconscious", 0.4, rerank=0.45),
    ]
    _patch_stages(monkeypatch, cands)

    async def run():
        ctx, ids, top = await mrs.retrieve_with_meta(None, "u-meta", [{"role": "user", "content": "q"}])
        return ctx, ids, top

    ctx, ids, top = asyncio.run(run())
    assert isinstance(ctx, str) and ctx == "(CTX n=4)", ctx
    assert sorted(ids) == ["c1", "c2", "e1", "s1"], ids
    assert abs(top - 0.9) < 1e-9, top


def test_text_channel_unchanged_dual_path(monkeypatch):
    """retrieve_and_build_context == retrieve_with_meta[0]（dual-path reconcile）。"""
    cands = [_mk("c1", "concept", 1.0, rerank=0.9)]
    _patch_stages(monkeypatch, cands)

    async def run():
        plain = await mrs.retrieve_and_build_context(None, "u-dual", [{"role": "user", "content": "q"}])
        meta = await mrs.retrieve_with_meta(None, "u-dual", [{"role": "user", "content": "q"}])
        return plain, meta[0]

    plain, meta_ctx = asyncio.run(run())
    assert plain == meta_ctx == "(CTX n=1)", (plain, meta_ctx)
    assert isinstance(plain, str)


def test_meta_empty_path(monkeypatch):
    _patch_stages(monkeypatch, [])

    async def run():
        return await mrs.retrieve_with_meta(None, "u-empty", [{"role": "user", "content": "q"}])

    ctx, ids, top = asyncio.run(run())
    assert ctx == "(CTX n=0)"
    assert ids == []
    assert top == 0.0


def test_cache_hit_returns_meta_without_db(monkeypatch):
    """会话级缓存命中不得丢失 meta（vmem 跨轮去重/仲裁依赖 ids+score）；
    命中路径不得触碰数据库。"""
    import time as _time
    from app.services import memory_retrieval_service as mrs

    monkeypatch.setattr(mrs, "_emit_recall_log", lambda *a, **k: None)
    uid = "u-cache-meta"
    msgs = [{"role": "user", "content": "缓存命中查询丙"}]
    key = mrs._session_cache_key(uid, ["缓存命中查询丙"])
    stored = ("CTX-CACHE", ["id-c1", "id-c2"], 0.83)
    mrs._session_cache[key] = (_time.time(), stored)
    try:
        async def run():
            return await mrs.retrieve_with_meta(None, uid, msgs)  # db=None：命中不得触库
        out = asyncio.run(run())
        assert out == stored, out
    finally:
        mrs._session_cache.pop(key, None)


def test_temporal_list_returns_ids(monkeypatch):
    """temporal_list 捷径须返回真实候选 id + top=1.0（语音仲裁/去重可用）。"""
    async def fake_stage0(query_text, user_id=None, db=None):
        return Stage0Result(keywords=["上周"], query_type="temporal_list",
                            include_expired=False)

    async def fake_shortcut(db, uid, stage0):
        return "CTX-T", ["ep-1", "ep-2"], 1.0

    monkeypatch.setattr(mrs, "_stage0_query_expansion", fake_stage0)
    monkeypatch.setattr(mrs, "_temporal_list_shortcut", fake_shortcut)
    monkeypatch.setattr(mrs, "_session_cache_get", lambda key: None)
    monkeypatch.setattr(mrs, "_session_cache_put", lambda key, value, meta=None, stats=None: None)
    monkeypatch.setattr(mrs, "_emit_recall_log", lambda *a, **k: None)

    out = asyncio.run(mrs.retrieve_with_meta(None, "u-tmp", [{"role": "user", "content": "上周我们聊了什么"}]))
    assert out == ("CTX-T", ["ep-1", "ep-2"], 1.0), out


def test_temporal_list_shortcut_real_db_returns_ids():
    """真实 DB：temporal_list 捷径的选择行必须带出 id（防 SELECT 缺列致 id 恒空）。"""
    import uuid
    from sqlalchemy import text as sql_text
    from app.db.database import AsyncSessionLocal
    uid = str(uuid.uuid4())

    async def run():
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(sql_text(
                    "INSERT INTO users (id, username, password_hash, role, is_active) "
                    "VALUES (:id, :un, 'x', 'user', TRUE)"),
                    {"id": uid, "un": f"tlist_{uid[:8]}"})
                await db.execute(sql_text(
                    "INSERT INTO memory_episodes (id, user_id, narrative, source_unit_ids, source_concept_ids, embedding, created_at) "
                    "VALUES (:id, :uid, :n, '[]', '[]', NULL, NOW())"),
                    {"id": str(uuid.uuid4()), "uid": uid, "n": "上周聊过音乐"})
                await db.commit()
                stage0 = Stage0Result(keywords=["上周"], query_type="temporal_list",
                                      include_expired=False)
                from app.services.memory_retrieval_service import _temporal_list_shortcut
                return await _temporal_list_shortcut(db, uid, stage0)
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(sql_text("DELETE FROM memory_episodes WHERE user_id=:u"), {"u": uid})
                await db.execute(sql_text("DELETE FROM users WHERE id=:u"), {"u": uid})
                await db.commit()

    ctx, ids, top = asyncio.run(run())
    assert ctx and ids, f"temporal 捷径必须返回 ids（缺 id 列则 SELECT 退化）got {ids}"
    assert top == 1.0
