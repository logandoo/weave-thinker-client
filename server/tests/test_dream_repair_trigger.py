"""W3 续波 A3（记忆/长程 v2，2026-10-24，判据 #54-#58）：

dream probe/repair 运行时触发——`run_consolidation` clean-pass 尾部消费
`run_probe_repair`/`check_stop_rule`（判据 #17 面），默认关
（`[memory] dream_repair_enabled=false`），savepoint 隔离（吞异常≠事务恢复，
memory 教训 3）。
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_consolidation_service as mcs  # noqa: E402


class _Res:
    def fetchone(self):
        return None


class _DB:
    """最小 FakeDB：consolidation 步骤已 monkeypatch，只剩尾部 UPDATE/commit。
    begin_nested=savepoint 接缝（_isolated_merge_item 用）。"""

    def __init__(self):
        self.sqls = []
        self.committed = 0

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return _Res()

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        pass

    def begin_nested(self):
        db = self

        class _Nested:
            async def __aenter__(self_inner):
                return db

            async def __aexit__(self_inner, *exc):
                return False

        return _Nested()


def _patch_pipeline(monkeypatch, *, fail_step=None):
    """把 run_consolidation 的六步/前置全部换掉——本测试只钉装配决策面。"""
    async def _ok_state(db, uid):
        return {"last_consolidation_at": None, "total_concept_count": 0, "meta": {}}

    async def _should(db, uid, state):
        return True

    async def _step(db, uid, result):
        if fail_step and result.get("_fail") is None:
            result["_fail"] = True
            raise RuntimeError("step boom")

    async def _dreaming(db, uid):
        return None

    async def _clear(db, uid, keep_error=False):
        return None

    async def _bump(db, uid):
        return 3

    monkeypatch.setattr(mcs, "_get_agent_state", _ok_state)
    monkeypatch.setattr(mcs, "_should_consolidate", _should)
    monkeypatch.setattr(mcs, "_try_active_dreaming", _dreaming)
    monkeypatch.setattr(mcs, "_clear_consolidation_failure", _clear)
    monkeypatch.setattr(mcs, "_bump_consolidation_failure", _bump)
    for name in ("_dedup_concepts_and_episodes", "_rebalance_clusters",
                 "_update_relations", "_run_weight_decay",
                 "_update_cluster_weights", "_generate_dream"):
        monkeypatch.setattr(mcs, name, _step)


def _patch_trigger(monkeypatch, calls):
    async def _trigger(db, uid):
        calls.append(uid)
        return True

    monkeypatch.setattr(mcs, "_try_dream_probe_repair", _trigger)


# ---------------- 判据 #54/#56：clean-pass 装配决策 ----------------

def test_clean_pass_triggers_probe_repair(monkeypatch):
    _patch_pipeline(monkeypatch)
    calls = []
    _patch_trigger(monkeypatch, calls)
    db = _DB()
    result = asyncio.run(mcs.run_consolidation(db, "u-a3"))
    assert calls == ["u-a3"], "clean-pass 尾部必须触发 dream probe/repair"
    assert db.committed >= 1


def test_failed_consolidation_does_not_trigger(monkeypatch):
    _patch_pipeline(monkeypatch, fail_step=True)
    calls = []
    _patch_trigger(monkeypatch, calls)
    db = _DB()
    asyncio.run(mcs.run_consolidation(db, "u-a3"))
    assert calls == [], "had_failure 分支不得触发（部分回滚状态不跑附加写入）"


# ---------------- 判据 #54/#55/#57：helper 门 + stop 规则 ----------------

def test_helper_gate_off_is_zero_call(monkeypatch):
    import app.services.memory_dream_repair_service as drs

    calls = []

    async def _probe(db, uid, enabled=False, probe_count=6):
        calls.append(("probe", enabled))
        return {"probe_count": 1}

    async def _stop(db, uid, window_days=14, min_reports=1):
        calls.append(("stop", window_days))
        return False

    monkeypatch.setattr(drs, "run_probe_repair", _probe)
    monkeypatch.setattr(drs, "check_stop_rule", _stop)
    from app.core.config import get_config
    mem = get_config().memory
    saved = mem.get("dream_repair_enabled", "unset")
    try:
        # 显式置关（评审 A#6：不赌环境默认——config.toml 若带 true 也钉零调用）
        mem["dream_repair_enabled"] = False

        async def _run():
            return await mcs._try_dream_probe_repair(_DB(), "u-a3")
        assert asyncio.run(_run()) is None
    finally:
        if saved == "unset":
            mem.pop("dream_repair_enabled", None)
        else:
            mem["dream_repair_enabled"] = saved
    assert calls == [], "门关：零调用零落库"


def test_helper_runs_probe_then_stop_rule(monkeypatch):
    import app.services.memory_dream_repair_service as drs

    calls = []

    async def _probe(db, uid, enabled=False, probe_count=6):
        calls.append(("probe", enabled))
        return {"probe_count": 1}

    async def _stop(db, uid, window_days=14, min_reports=1):
        calls.append(("stop", window_days, min_reports))
        return False

    monkeypatch.setattr(drs, "run_probe_repair", _probe)
    monkeypatch.setattr(drs, "check_stop_rule", _stop)
    from app.core.config import get_config
    mem = get_config().memory
    saved = mem.get("dream_repair_enabled", "unset")
    try:
        mem["dream_repair_enabled"] = True

        async def _run():
            return await mcs._try_dream_probe_repair(_DB(), "u-a3")
        out = asyncio.run(_run())
    finally:
        if saved == "unset":
            mem.pop("dream_repair_enabled", None)
        else:
            mem["dream_repair_enabled"] = saved
    assert out is True
    assert calls == [("probe", True), ("stop", 14, 3)], \
        "先 probe/repair 后 stop 规则（14 天窗 + min_reports=3 观察下限）"


def test_helper_skips_stop_when_no_report(monkeypatch):
    """评审 A#1 修批：probe 短路/无探测词（None）→ stop 规则不得拿陈旧
    报告重复重判（防 stop 行重复 + 恢复陷阱加剧）。"""
    import app.services.memory_dream_repair_service as drs

    calls = []

    async def _probe(db, uid, enabled=False, probe_count=6):
        calls.append("probe")
        return None

    async def _stop(db, uid, window_days=14, min_reports=1):
        calls.append("stop")

    monkeypatch.setattr(drs, "run_probe_repair", _probe)
    monkeypatch.setattr(drs, "check_stop_rule", _stop)
    from app.core.config import get_config
    mem = get_config().memory
    saved = mem.get("dream_repair_enabled", "unset")
    try:
        mem["dream_repair_enabled"] = True

        async def _run():
            return await mcs._try_dream_probe_repair(_DB(), "u-a3")
        assert asyncio.run(_run()) is False
    finally:
        if saved == "unset":
            mem.pop("dream_repair_enabled", None)
        else:
            mem["dream_repair_enabled"] = saved
    assert calls == ["probe"]


def test_helper_fail_open_savepoint(monkeypatch):
    """判据 #56：probe 抛异常 → 静默隔离，不影响 consolidation 事务；
    stop 规则不再执行（调用捕获断言，防假绿）。"""
    import app.services.memory_dream_repair_service as drs

    calls = []

    async def _boom(db, uid, enabled=False, probe_count=6):
        calls.append("probe")
        raise RuntimeError("probe boom")

    async def _stop(db, uid, window_days=14, min_reports=1):
        calls.append("stop")

    monkeypatch.setattr(drs, "run_probe_repair", _boom)
    monkeypatch.setattr(drs, "check_stop_rule", _stop)
    from app.core.config import get_config
    mem = get_config().memory
    saved = mem.get("dream_repair_enabled", "unset")
    try:
        mem["dream_repair_enabled"] = True

        async def _run():
            return await mcs._try_dream_probe_repair(_DB(), "u-a3")
        assert asyncio.run(_run()) is False
    finally:
        if saved == "unset":
            mem.pop("dream_repair_enabled", None)
        else:
            mem["dream_repair_enabled"] = saved
    assert calls == ["probe"], "probe 失败后 stop 规则不得执行"


# ---------------- 判据 #57：Config 属性单一真源 ----------------

def test_dream_repair_config_property():
    from app.core.config import get_config

    cfg = get_config()
    saved = cfg.memory.get("dream_repair_enabled", "unset")
    try:
        cfg.memory.pop("dream_repair_enabled", None)
        assert cfg.memory_dream_repair_enabled is False, "默认关（P3）"
        cfg.memory["dream_repair_enabled"] = True
        assert cfg.memory_dream_repair_enabled is True
        cfg.memory["dream_repair_enabled"] = "false"
        assert cfg.memory_dream_repair_enabled is False, "字符串 fail-safe"
        cfg.memory["dream_repair_enabled"] = "true"
        assert cfg.memory_dream_repair_enabled is True
    finally:
        if saved == "unset":
            cfg.memory.pop("dream_repair_enabled", None)
        else:
            cfg.memory["dream_repair_enabled"] = saved


# ---------------- 判据 #54 端到端（PG）：helper 走真实服务 ----------------

def test_helper_pg_end_to_end():
    dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
    try:
        from app.core.config import get_config
        dsn = dsn or get_config().database_url
    except Exception:
        pass
    if not dsn:
        pytest.skip("no PG DSN")
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.database import (Base, Conversation, ConversationGroup,
                                 DreamRepairReport, MemoryConcept,
                                 MemoryEvalRun, Message, User)

    async def _run():
        eng = create_async_engine(dsn)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(
                    c, tables=[Base.metadata.tables[n] for n in
                               ("users", "conversation_groups", "conversations",
                                "messages", "memory_concepts",
                                "dream_repair_reports", "memory_eval_runs")],
                    checkfirst=True))
        except Exception as exc:
            pytest.skip(f"PG unavailable: {type(exc).__name__}")
        Session = async_sessionmaker(eng, expire_on_commit=False)
        uid = "a3-" + os.urandom(4).hex()
        try:
            async with Session() as db:
                db.add(User(id=uid, username=uid, password_hash="x"))
                db.add(ConversationGroup(id=uid + "-g", user_id=uid, name="g"))
                await db.flush()
                db.add(Conversation(id=uid + "-c", user_id=uid,
                                    group_id=uid + "-g", title="t"))
                await db.flush()
                db.add(Message(conversation_id=uid + "-c", role="user",
                               content="我们把「站会」改在 10:15"))
                await db.commit()
            from app.core.config import get_config
            mem = get_config().memory
            saved = mem.get("dream_repair_enabled", "unset")
            mem["dream_repair_enabled"] = True
            try:
                async with Session() as db:
                    eval_before = set((await db.execute(
                        select(MemoryEvalRun.id))).scalars().all())
                    ok = await mcs._try_dream_probe_repair(db, uid)
                    await db.commit()
            finally:
                if saved == "unset":
                    mem.pop("dream_repair_enabled", None)
                else:
                    mem["dream_repair_enabled"] = saved
            assert ok is True
            async with Session() as db:
                reps = (await db.execute(select(DreamRepairReport).where(
                    DreamRepairReport.user_id == uid))).scalars().all()
                assert reps, "dream_repair_reports 必须落库（判据 #54）"
                stops = (await db.execute(select(MemoryEvalRun).where(
                    MemoryEvalRun.suite == "dream_repair_stop",
                    MemoryEvalRun.arm == uid))).scalars().all()
                assert stops == [], "单次运行无 14 天窗数据不应落 stop"
                # 评审 A#5/B-Minor 修批：eval 行按实测 id 增量删（run 行
                # arm="probes=N"，全前缀删会漏/全表删会毁他人遥测）
                mine = [r.id for r in (await db.execute(
                    select(MemoryEvalRun).where(
                        MemoryEvalRun.run_id.like("dream-repair%")))).scalars().all()
                    if r.id not in eval_before]
                await db.execute(delete(MemoryEvalRun).where(
                    MemoryEvalRun.id.in_(mine)))
                await db.execute(delete(MemoryEvalRun).where(
                    MemoryEvalRun.arm == uid))
                await db.execute(delete(DreamRepairReport).where(
                    DreamRepairReport.user_id == uid))
                await db.execute(delete(MemoryConcept).where(
                    MemoryConcept.user_id == uid))
                await db.execute(delete(MemoryEvalRun).where(
                    MemoryEvalRun.arm == uid))
                await db.execute(delete(Message).where(
                    Message.conversation_id == uid + "-c"))
                await db.execute(delete(Conversation).where(
                    Conversation.id == uid + "-c"))
                await db.execute(delete(ConversationGroup).where(
                    ConversationGroup.id == uid + "-g"))
                await db.execute(delete(User).where(User.id == uid))
                await db.commit()
        finally:
            await eng.dispose()

    asyncio.run(_run())


def test_helper_pg_savepoint_isolation():
    """判据 #56 事务面（评审 A#3 修批）：probe 中途写入后抛异常 → savepoint
    回滚该单元，**外层事务仍可继续提交**（历史事故类：吞异常≠事务恢复）。"""
    dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
    try:
        from app.core.config import get_config
        dsn = dsn or get_config().database_url
    except Exception:
        pass
    if not dsn:
        pytest.skip("no PG DSN")
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.database import Base, MemoryConcept, User

    async def _run():
        eng = create_async_engine(dsn)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(
                    c, tables=[Base.metadata.tables[n] for n in
                               ("users", "memory_concepts")],
                    checkfirst=True))
        except Exception as exc:
            pytest.skip(f"PG unavailable: {type(exc).__name__}")
        Session = async_sessionmaker(eng, expire_on_commit=False)
        uid = "a3sp-" + os.urandom(4).hex()
        import app.services.memory_dream_repair_service as drs
        from app.core.config import get_config

        async def _poison_probe(db, user_id, enabled=False, probe_count=6):
            db.add(MemoryConcept(user_id=user_id, canonical_name="poison-" + uid,
                                 description_short="s", source_trust="agent_inferred",
                                 memory_type="semantic", weight=0.1))
            await db.flush()
            raise RuntimeError("probe mid-write boom")

        orig = drs.run_probe_repair
        mem = get_config().memory
        saved = mem.get("dream_repair_enabled", "unset")
        drs.run_probe_repair = _poison_probe
        mem["dream_repair_enabled"] = True
        try:
            async with Session() as db:
                db.add(User(id=uid, username=uid, password_hash="x"))
                await db.commit()
            async with Session() as db:
                # 哨兵（评审 A#3/B#6 修批）：probe 前的外层未提交写必须在
                # probe 失败后幸存——区分 savepoint 回滚 vs 全量 db.rollback
                db.add(User(id=uid + "-sentinel", username=uid + "-sentinel",
                            password_hash="x"))
                await db.flush()
                out = await mcs._try_dream_probe_repair(db, uid)
                assert out is False, "probe 失败返回 False（fail-open）"
                # 外层事务必须未被毒化：可继续写并提交
                db.add(User(id=uid + "-after", username=uid + "-after",
                            password_hash="x"))
                await db.commit()
            async with Session() as db:
                poison = (await db.execute(select(MemoryConcept).where(
                    MemoryConcept.canonical_name == "poison-" + uid))).scalars().all()
                assert poison == [], "savepoint 必须回滚 probe 的半截写入"
                sentinel = (await db.execute(select(User).where(
                    User.id == uid + "-sentinel"))).scalars().all()
                assert sentinel, "哨兵幸存=savepoint 语义（非全量 rollback）"
                survivor = (await db.execute(select(User).where(
                    User.id == uid + "-after"))).scalars().all()
                assert survivor, "外层事务可续提交（判据 #56）"
        finally:
            drs.run_probe_repair = orig
            if saved == "unset":
                mem.pop("dream_repair_enabled", None)
            else:
                mem["dream_repair_enabled"] = saved
            async with Session() as db:
                await db.execute(delete(MemoryConcept).where(
                    MemoryConcept.user_id == uid))
                await db.execute(delete(User).where(
                    User.id.in_([uid, uid + "-after", uid + "-sentinel"])))
                await db.commit()
            await eng.dispose()

    asyncio.run(_run())


def test_check_stop_rule_min_reports_enforced():
    """判据 #55 观察下限（评审 A#2/B#1 blocking 修批钉）：min_reports 必须
    真正执行——2 份零增益报告 + min_reports=3 不判停；第 3 份才落 stop。"""
    dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
    try:
        from app.core.config import get_config
        dsn = dsn or get_config().database_url
    except Exception:
        pass
    if not dsn:
        pytest.skip("no PG DSN")
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.database import Base, DreamRepairReport, MemoryEvalRun, User
    from app.services import memory_dream_repair_service as drs

    async def _run():
        eng = create_async_engine(dsn)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(lambda c: Base.metadata.create_all(
                    c, tables=[Base.metadata.tables[n] for n in
                               ("users", "dream_repair_reports",
                                "memory_eval_runs")],
                    checkfirst=True))
        except Exception as exc:
            pytest.skip(f"PG unavailable: {type(exc).__name__}")
        Session = async_sessionmaker(eng, expire_on_commit=False)
        uid = "a3min-" + os.urandom(4).hex()
        try:
            async with Session() as db:
                db.add(User(id=uid, username=uid, password_hash="x"))
                await db.flush()
                for _ in range(2):
                    db.add(DreamRepairReport(
                        user_id=uid, run_date="2026-10-24", probe_count=2,
                        hit_count=2, miss_count=0, repairs_json=[],
                        hit_at_k_before=1.0, hit_at_k_after=1.0,
                        cost_tokens=0))
                await db.commit()
            async with Session() as db:
                assert await drs.check_stop_rule(db, uid, min_reports=3) is False, \
                    "2 份报告 < min_reports=3：不判停（防第 1 天误杀）"
                await db.commit()
            async with Session() as db:
                stops = (await db.execute(select(MemoryEvalRun).where(
                    MemoryEvalRun.suite == "dream_repair_stop",
                    MemoryEvalRun.arm == uid))).scalars().all()
                assert stops == [], "不足观察下限不得落 stop 行"
                db.add(DreamRepairReport(
                    user_id=uid, run_date="2026-10-24", probe_count=2,
                    hit_count=2, miss_count=0, repairs_json=[],
                    hit_at_k_before=1.0, hit_at_k_after=1.0,
                    cost_tokens=0))
                await db.commit()
            async with Session() as db:
                assert await drs.check_stop_rule(db, uid, min_reports=3) is True, \
                    "3 份零增益报告 ≥ 下限：判停（回退方案 B）"
                await db.commit()
        finally:
            async with Session() as db:
                await db.execute(delete(MemoryEvalRun).where(
                    MemoryEvalRun.arm == uid))
                await db.execute(delete(DreamRepairReport).where(
                    DreamRepairReport.user_id == uid))
                await db.execute(delete(User).where(User.id == uid))
                await db.commit()
            await eng.dispose()

    asyncio.run(_run())
