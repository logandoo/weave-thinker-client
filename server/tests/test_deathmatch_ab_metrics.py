"""W4-2 四开关 A/B shadow 指标（记忆/长程 v2，2026-10-23，判据 #19）。

判据 #19：evidence_units / consensus / reflection_retract / memory_injection
四开关的 shadow 指标（reopen 率/重复工具率/时长/轮次）可采集入
memory_eval_runs（suite='deathmatch_ab'）并按 arm 聚合进 summarize。
服务只提供数据；负向结果写回 memory/ ❌（决策记录）是人工流程。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_deathmatch_ab_metrics.py -v
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSwitchState:
    """纯逻辑：开关状态读取 + arm 字符串稳定排序（≤64 列宽）。"""

    def test_reads_deathmatch_dict(self):
        from app.services.deathmatch_ab_service import current_switch_state

        st = current_switch_state({"evidence_units_enabled": True,
                                   "consensus_challenger_enabled": False,
                                   "reflection_retract_enabled": True,
                                   "memory_injection_enabled": False})
        assert st == {"evidence_units": True, "consensus": False,
                      "reflection_retract": True, "memory_injection": False}

    def test_defaults_off_shadow(self):
        from app.services.deathmatch_ab_service import current_switch_state

        assert current_switch_state({}) == {
            "evidence_units": False, "consensus": False,
            "reflection_retract": False, "memory_injection": False}
        # 也接受外层 config dict（含 "deathmatch" 子 dict）
        assert current_switch_state(
            {"deathmatch": {"consensus_challenger_enabled": True}}
        )["consensus"] is True

    def test_arm_string_stable(self):
        from app.services.deathmatch_ab_service import arm_label

        a = arm_label({"memory_injection": True, "evidence_units": False,
                       "consensus": False, "reflection_retract": False})
        b = arm_label({"evidence_units": False, "consensus": False,
                       "reflection_retract": False, "memory_injection": True})
        assert a == b, "arm 字符串与 dict 插入顺序无关（稳定排序）"
        assert a == "eu=off,co=off,rr=off,mi=on"
        assert len(a) <= 64, "arm 列宽 String(64) 上限"


class TestAbMetrics:
    """PG 集成：两臂各 2 条 → summarize 分组均值正确。"""

    def test_record_and_summarize(self):
        dsn = os.environ.get("WT_TEST_PG_URL", "").strip()
        try:
            from app.core.config import get_config
            dsn = dsn or get_config().database_url
        except Exception:
            pass
        if not dsn:
            pytest.skip("no PG DSN")
        from sqlalchemy.ext.asyncio import create_async_engine

        async def _run():
            from sqlalchemy import delete, select

            from app.db.database import Base, MemoryEvalRun
            from app.services import deathmatch_ab_service as abs_

            eng = create_async_engine(dsn)
            try:
                async with eng.begin() as conn:
                    await conn.run_sync(lambda c: Base.metadata.create_all(
                        c, tables=[Base.metadata.tables["memory_eval_runs"]],
                        checkfirst=True))
            except Exception as exc:
                pytest.skip(f"PG unavailable: {type(exc).__name__}")
            from sqlalchemy.ext.asyncio import async_sessionmaker
            Session = async_sessionmaker(eng, expire_on_commit=False)
            cid = "w42-" + os.urandom(4).hex()
            state_on = {"evidence_units": True, "consensus": False,
                        "reflection_retract": False, "memory_injection": False}
            state_off = {"evidence_units": False, "consensus": True,
                         "reflection_retract": False, "memory_injection": False}
            try:
                # ① 采集：两臂各 2 条（指标面：reopen 率/重复工具率/时长/轮次）
                async with Session() as db:
                    await abs_.record_ab_metrics(db, cid, {
                        "switch_state": state_on, "reopen_rate": 0.2,
                        "duplicate_tool_rate": 0.1, "duration_seconds": 100,
                        "turns": 5})
                    await abs_.record_ab_metrics(db, cid, {
                        "switch_state": state_on, "reopen_rate": 0.4,
                        "duplicate_tool_rate": 0.3, "duration_seconds": 200,
                        "turns": 7})
                    await abs_.record_ab_metrics(db, cid, {
                        "switch_state": state_off, "reopen_rate": 0.6,
                        "duplicate_tool_rate": 0.5, "duration_seconds": 300,
                        "turns": 9})
                    await abs_.record_ab_metrics(db, cid, {
                        "switch_state": state_off, "reopen_rate": 0.8,
                        "duplicate_tool_rate": 0.7, "duration_seconds": 500,
                        "turns": 11})

                # ② 落库面核对：suite/arm/metrics_json
                async with Session() as db:
                    rows = (await db.execute(select(MemoryEvalRun).where(
                        MemoryEvalRun.suite == "deathmatch_ab",
                        MemoryEvalRun.metrics_json["conversation_id"].astext == cid)
                    )).scalars().all()
                assert len(rows) == 4
                arms_seen = {r.arm for r in rows}
                assert arms_seen == {"eu=on,co=off,rr=off,mi=off",
                                     "eu=off,co=on,rr=off,mi=off"}, \
                    f"arm 必须是稳定字符串，got {arms_seen}"
                assert all(isinstance(r.metrics_json, dict) for r in rows)
                assert all("switch_state" in r.metrics_json for r in rows)

                # ③ summarize：按 arm 聚合均值
                async with Session() as db:
                    out = await abs_.summarize_ab(db, days=14)
                assert out["days"] == 14
                by_arm = {a["arm"]: a for a in out["arms"]}
                a_on = by_arm["eu=on,co=off,rr=off,mi=off"]
                assert a_on["count"] == 2
                assert abs(a_on["reopen_rate"] - 0.3) < 1e-9
                assert abs(a_on["duplicate_tool_rate"] - 0.2) < 1e-9
                assert abs(a_on["duration_seconds"] - 150.0) < 1e-9
                assert abs(a_on["turns"] - 6.0) < 1e-9
                a_off = by_arm["eu=off,co=on,rr=off,mi=off"]
                assert a_off["count"] == 2
                assert abs(a_off["reopen_rate"] - 0.7) < 1e-9
                assert abs(a_off["duplicate_tool_rate"] - 0.6) < 1e-9
                assert abs(a_off["duration_seconds"] - 400.0) < 1e-9
                assert abs(a_off["turns"] - 10.0) < 1e-9
                assert [a["arm"] for a in out["arms"]] == sorted(
                    a["arm"] for a in out["arms"]), "arm 聚合输出稳定排序"
                assert out["total"] == 4
            finally:
                async with Session() as db:
                    await db.execute(delete(MemoryEvalRun).where(
                        MemoryEvalRun.suite == "deathmatch_ab",
                        MemoryEvalRun.metrics_json["conversation_id"].astext == cid))
                    await db.commit()
                await eng.dispose()

        asyncio.run(_run())
