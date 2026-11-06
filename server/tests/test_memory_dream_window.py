"""2026-08-25 consolidation dream 时间窗修复测试（fake DB + stub LLM，离线可跑）。

根因（60 服务器 alice 实证）：_generate_dream 的 top/bottom10 取全历史概念
（无时间窗、平局无次级排序），prompt 却框定"生成今日记忆整理 dream"，
LLM 把 07-29 迁移导入、数月未再出现的旧主题（夜校/血吸虫/服务器运维）
叙述成"当前的边缘性主题"。

修复语义：
- top/bottom 限定近 dream_concept_window_days 天（last_recalled_at 或 created_at 在窗内）
- 新增"待遗忘候选"名单 = 超过窗口未再出现的活跃概念（prompt 约束 LLM 只可一句带过）
- 平局次级排序 last_recalled_at DESC NULLS LAST（权重齐 1.0 时顺序确定）

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_dream_window.py -v
"""
import asyncio
import json

import pytest

from app.core.config import get_config
import app.services.memory_consolidation_service as cons


class _R:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None


class FakeDreamSession:
    def __init__(self):
        self.sqls = []
        self.insert_params = None
        self.update_params = None

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sqls.append(sql)
        p = params or {}
        if "FROM memory_clarifications" in sql:
            return _R([])
        # W1-3/W3-2（2026-10-23）：dream 写入前收集来源 source_trust（派生
        # taint 继承最严——fixture 补新 SQL 面，不动既有断言）
        if "FROM memory_concepts" in sql and "DISTINCT source_trust" in sql:
            return _R([('user_stated',)])
        if "FROM memory_concepts" in sql and "LIMIT 30" in sql:
            return _R([('今日新概念X',)])
        if "FROM memory_concepts" in sql and "COALESCE(last_recalled_at, created_at) < :since" in sql:
            return _R([('夜校Agent教学', '0.30'), ('血吸虫钉螺预警系统', '0.12')])
        if "FROM memory_concepts" in sql and "DESC, last_recalled_at DESC NULLS LAST" in sql:
            return _R([('WeaveThinker本地AI笔记系统', '1.00'), ('AMD AI Max 395平台LLM部署优化', '1.00')])
        if "FROM memory_concepts" in sql and "ASC, last_recalled_at DESC NULLS LAST" in sql:
            return _R([('近窗低频主题Y', '0.55')])
        if "FROM memory_concepts" in sql and "COUNT(*)" in sql:
            return _R([109])
        if "FROM memory_clusters" in sql:
            return _R([('WeaveThinker相关', 12)])
        if "SELECT id FROM user_agent_states" in sql:
            return _R([('sid-1',)])
        if "SELECT 1 FROM agent_dreams" in sql:
            return _R([])
        if "INSERT INTO agent_dreams" in sql:
            self.insert_params = p
            return _R([1])
        if "UPDATE user_agent_states SET latest_dream_id" in sql:
            self.update_params = p
            return _R([1])
        raise AssertionError(f"unexpected SQL: {sql[:120]}")

    async def commit(self):
        pass


STUB_DREAM_JSON = json.dumps({
    "date": "2026-08-25",
    "new_concepts": ["今日新概念X"],
    "merged_concepts": [],
    "clarifications_applied": 0,
    "patterns_observed": "近窗焦点：WeaveThinker/AMD 大模型部署。",
    "attention_needed": [],
    "narrative": "测试叙述：近14天焦点为 WeaveThinker 与 AMD 部署调优；旧主题夜校Agent教学、血吸虫钉螺预警系统正在渐淡。",
}, ensure_ascii=False)


@pytest.fixture
def stub_llm(monkeypatch):
    calls = []

    def _llm(kind):
        class _LLM:
            async def complete_chat(self, messages, **kw):
                calls.append({"kind": kind, "messages": messages})
                return STUB_DREAM_JSON
        return _LLM()

    monkeypatch.setattr("app.services.memory_llm_factory._memory_llm", _llm)
    return calls


def _run():
    db = FakeDreamSession()
    result = {"dedup_merged": 0, "cold_forgotten": 2}
    asyncio.run(cons._generate_dream(db, "u1", result))
    return db, result


def test_dream_llm_payload_has_windowed_lists(stub_llm):
    db, result = _run()
    assert result["dream_generated"] is True
    call = stub_llm[0]
    assert call["kind"] == "dream"
    payload = json.loads(call["messages"][1]["content"])
    w = int(get_config().memory.get("dream_concept_window_days", 14))
    top_key = f"近{w}天高频概念_当前焦点"
    bottom_key = f"近{w}天低频概念"
    forget_key = f"待遗忘候选_近{w}天未再出现"
    assert top_key in payload and [c["name"] for c in payload[top_key]] == \
        ['WeaveThinker本地AI笔记系统', 'AMD AI Max 395平台LLM部署优化']
    assert [c["name"] for c in payload[bottom_key]] == ['近窗低频主题Y']
    assert [c["name"] for c in payload[forget_key]] == \
        ['夜校Agent教学', '血吸虫钉螺预警系统']
    assert payload["今日新增概念"] == ['今日新概念X']
    assert payload["概念集合"] == [{"name": "WeaveThinker相关", "members": 12}]
    assert payload["冷遗忘数"] == 2


def test_dream_prompt_constrains_stale_topics(stub_llm):
    db, _ = _run()
    system = stub_llm[0]["messages"][0]["content"]
    assert "绝不能把它们描述为近期活动" in system
    assert "渐淡" in system
    # A4.9 M3a：近窗全空时不得虚构焦点
    assert "不得虚构焦点" in system


def test_dream_queries_carry_window_and_tiebreak(stub_llm):
    db, _ = _run()
    top_sql = [s for s in db.sqls if "DESC, last_recalled_at DESC NULLS LAST" in s]
    bottom_sql = [s for s in db.sqls if "ASC, last_recalled_at DESC NULLS LAST" in s]
    forget_sql = [s for s in db.sqls if "COALESCE(last_recalled_at, created_at) < :since" in s]
    assert len(top_sql) == 1 and len(bottom_sql) == 1 and len(forget_sql) == 1
    # top/bottom 只能取近窗内活跃概念（last_recalled_at 或 created_at 在窗内），
    # 且排除 profile——防止旧迁移概念进主叙述
    for s in top_sql + bottom_sql:
        assert "last_recalled_at >= :since OR created_at >= :since" in s
        assert "memory_type != 'profile'" in s
    # 待遗忘候选 = 超出窗口的活跃概念（不再要求"曾在窗内"）
    assert "status = 'active'" in forget_sql[0]
    assert "last_recalled_at >= :since" not in forget_sql[0]


def test_dream_insert_uses_llm_narrative(stub_llm):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expected_today = datetime.now(ZoneInfo(get_config().memory_timezone)).strftime("%Y-%m-%d")
    db, _ = _run()
    assert db.insert_params is not None
    assert db.insert_params["sum"].startswith("测试叙述：")
    assert db.insert_params["cc"] == 109
    # 桩里的日期 2026-08-25 超出 ±3 天时会被 _sanitize_dream_date 回退为今天
    assert db.insert_params["dt"] in ("2026-08-25", expected_today)
    meta = json.loads(db.insert_params["meta"])
    assert meta["patterns_observed"].startswith("近窗焦点")
    assert db.update_params["did"] is not None
