"""2026-08-25 记忆成本治理修复测试（fake DB，离线可跑，无 pytest-asyncio 依赖）。

覆盖三个根因（60 服务器 alice 实证事故）：
1. 纯相对阈值（avg×warn）+ 低日均（~1）→ 正常 1-4 调用/天也升级并卡死
   → 绝对下限 min_today_calls
2. 完全静默用户 0 < 0×0.5 不成立 → 降级后永久卡死
   → (daily_avg<=0 and today_calls<=0) 恢复分支
3. 降级时 reason 被覆写为 "usage recovered" → UI"触发原因"自相矛盾
   → reason 保留（解释当前降级等级），降级动作记 last_change

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_cost_governance_logic.py -v
"""
import asyncio
import json
from datetime import datetime

import pytest

from app.core.config import get_config
import app.services.memory_cost_governance_service as cg


CG_KEYS = {
    "per_user_independent": True,
    "rolling_window_days": 7,
    "warn_multiplier": 5.0,
    "min_today_calls": 8,
    # A4.9 I1 终版：today ≤ avg×ratio 即恢复一级。ratio=1.0 ——
    # 0.5 在稳态 R 次/天用户下永不可达（today≈R ≥ 0.5R）→ 卡死。
    "recovery_ratio": 1.0,
    "degrade_steps": ["rerank_off", "profile_sync_off", "stage0_off"],
}


class _FakeResult:
    def __init__(self, value=0):
        self._value = value

    def scalar(self):
        return self._value

    def fetchone(self):
        return (self._value,) if self._value is not None else None


class FakeSession:
    """按 SQL 内容路由的伪 AsyncSession。

    today_calls / period_calls 控制两条 COUNT 查询的返回
    （按 since 距 now 的天数区分 7 日窗与 24h 窗）。
    """

    def __init__(self, meta: str, today_calls: int = 0, period_calls: int = 0):
        self.meta = meta
        self.today_calls = today_calls
        self.period_calls = period_calls
        self.updates = []

    async def execute(self, stmt, params=None):
        params = params or {}
        sql = str(stmt)
        if "SELECT COUNT(*) FROM memory_llm_calls" in sql:
            days = (datetime.utcnow() - params["since"]).days
            return _FakeResult(self.today_calls if days <= 1 else self.period_calls)
        if "SELECT metadata_json FROM user_agent_states" in sql:
            return _FakeResult(self.meta)
        if sql.startswith("UPDATE user_agent_states") and "metadata_json" in sql:
            self.updates.append(params["meta"])
            self.meta = params["meta"]
            return _FakeResult(1)
        return _FakeResult(None)

    async def commit(self):
        pass

    async def flush(self):
        pass


def _meta(level: int, reason: str = "") -> str:
    return json.dumps({"cost_governance": {"level": level, "reason": reason}})


def _stored(db) -> dict:
    return json.loads(db.updates[-1])["cost_governance"] if db.updates else {}


def _check(db):
    return cg.check_user_threshold_and_degrade(db, "u1")


@pytest.fixture(autouse=True)
def _patch_cg_config(monkeypatch):
    # config 是进程级 lru_cache 单例——保存/恢复原值，避免污染同 session 其他测试。
    # 但 `cg.config` 是**import 期捕获的模块级绑定**（memory_cost_governance_service:12
    # `config = get_config()`，全仓 70+ 模块同款约定）：任何先行测试调用过
    # clear_config_cache() 后，get_config() 返回的已是新实例，仅改
    # `get_config()._config` 不会被服务看见 → 本 fixture 必须把服务的 config 绑定
    # 一并对齐到自己改的那一份，否则断言会读到仓库真实配置（假红/假绿）。
    cfg = get_config()
    monkeypatch.setattr(cg, "config", cfg)
    mem = cfg._config.setdefault("memory", {})
    saved_enabled = mem.get("cost_governance_enabled")
    saved_cg = mem.get("cost_governance")
    mem["cost_governance_enabled"] = True
    mem["cost_governance"] = {k: (list(v) if isinstance(v, list) else v) for k, v in CG_KEYS.items()}
    yield
    if saved_enabled is None:
        mem.pop("cost_governance_enabled", None)
    else:
        mem["cost_governance_enabled"] = saved_enabled
    if saved_cg is None:
        mem.pop("cost_governance", None)
    else:
        mem["cost_governance"] = saved_cg


def test_normal_day_floor_blocks_escalation():
    """事故复刻：4 调用/天、7 日均值 1.0。旧值 warn=1.5 → 4>1.5 升级；
    绝对下限 min_today_calls=8 必须拦住（即使 warn 回落到旧值）。"""
    get_config()._config["memory"]["cost_governance"]["warn_multiplier"] = 1.5  # 旧生产值
    db = FakeSession(_meta(0), today_calls=4, period_calls=7)
    assert asyncio.run(_check(db)) == 0
    assert db.updates == []


def test_burst_escalates():
    """真失控 burst（08-10 回填 37 次）：37 > max(40/7×5.0, 8) = 28.6 → 升级 L1，
    reason 记录触发式，last_change 记 L0->L1。"""
    db = FakeSession(_meta(0), today_calls=37, period_calls=40)
    assert asyncio.run(_check(db)) == 1
    s = _stored(db)
    assert s["level"] == 1
    assert "today 37" in s["reason"]
    assert s["last_change"].startswith("L0->L1")


def test_silent_user_recovers():
    """完全静默（两窗都是 0）：旧逻辑 0 < 0×0.5 不成立 → 永久卡死。
    A4.9 I1 终版语义：avg=0 蕴含 today=0（24h 窗 ⊂ 7 日窗），0 ≤ 0×1.0 自然成立；
    恢复且保留原 reason（UI"触发原因"不再自相矛盾）。"""
    old_reason = "today 37 > 7d avg 5.7 x 5.0 (floor 8)"
    db = FakeSession(_meta(1, old_reason), today_calls=0, period_calls=0)
    assert asyncio.run(_check(db)) == 0
    s = _stored(db)
    assert s["reason"] == old_reason
    assert s["last_change"].startswith("L1->L0 usage recovered")


def test_escalation_climbs_second_step():
    """多步爬升：已 L1 且今晚仍是失控 burst（100 次 > 上限 85.7）→ L2，
    last_change 记录 L1->L2、reason 刷新为当前等级触发式。"""
    db = FakeSession(_meta(1, "today 37 > ..."), today_calls=100, period_calls=120)
    assert asyncio.run(_check(db)) == 2
    s = _stored(db)
    assert s["level"] == 2
    assert "today 100" in s["reason"]
    assert s["last_change"].startswith("L1->L2")


def test_frozen_night_no_movement():
    """冻结夜（A4.9 I2）：L1 残留 + 今天 3 次仍高于衰减后的 avg(0.43)——
    不满足升级上限（floor=8）也不满足恢复（3 ≤ 0.43×1.0 假）→ 原地不动、零写入。"""
    db = FakeSession(_meta(1, "burst"), today_calls=3, period_calls=3)
    assert asyncio.run(_check(db)) == 1
    assert db.updates == [], "冻结夜不得产生任何 metadata 写入"


def test_degrade_keeps_reason():
    """L2 → L1 正常降级：reason 保留（它是当前降级等级的解释），last_change 记 L2->L1。"""
    old_reason = "today 41 > 7d avg 6.0 x 5.0 (floor 8)"
    db = FakeSession(_meta(2, old_reason), today_calls=0, period_calls=5)
    assert asyncio.run(_check(db)) == 1
    s = _stored(db)
    assert s["reason"] == old_reason
    assert s["last_change"].startswith("L2->L1 usage recovered")


def test_active_user_no_spurious_degrade():
    """正常使用中的用户（0 调用 24h、周期内 7 日均值 ~1）：
    0 < 7/7×0.5 成立 → 若已降级则每天降一级；level 0 时不动。"""
    db = FakeSession(_meta(0), today_calls=0, period_calls=7)
    assert asyncio.run(_check(db)) == 0
    assert db.updates == []


def test_no_escalation_above_max_level():
    """已到 max_level（3 步 → L3）不再升级。"""
    db = FakeSession(_meta(3, "x"), today_calls=100, period_calls=100)
    assert asyncio.run(_check(db)) == 3
    assert db.updates == []


def test_status_exposes_last_change():
    last = "L2->L1 usage recovered (2026-08-24T16:00:10)"
    meta = json.dumps({"cost_governance": {
        "level": 1, "reason": "today 41 > ...", "last_change": last}})
    db = FakeSession(meta, today_calls=4, period_calls=7)
    st = asyncio.run(cg.get_user_degrade_status(db, "u1"))
    assert st["level"] == 1
    assert st["reason"] == "today 41 > ..."
    assert st["last_change"] == last
    assert st["disabled_steps"] == ["rerank_off"]


def test_reset_records_last_change():
    db = FakeSession(_meta(1, "old"), today_calls=0)
    asyncio.run(cg.reset_user_degrade("u1", db))
    s = _stored(db)
    assert s["level"] == 0
    assert s["reason"] == "manual reset"
    assert s["last_change"].startswith("L1->L0 manual reset")


def test_floor_boundary_exact():
    """边界：today=8（== floor 8）不升级（严格 >）；today=9 且 7 日均值低 → 升级。"""
    db = FakeSession(_meta(0), today_calls=8, period_calls=8)
    assert asyncio.run(_check(db)) == 0  # 8 不 > max(8/7×5, 8)=8
    db2 = FakeSession(_meta(0), today_calls=9, period_calls=9)
    assert asyncio.run(_check(db2)) == 1  # 9 > max(9/7×5≈6.4, 8)
