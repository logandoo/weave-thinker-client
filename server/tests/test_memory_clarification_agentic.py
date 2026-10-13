"""TDD tests: 记忆纠正检测全 agentic 化（2026-09-20，用户指令）。

背景：`memory_clarification_service._SIGNAL_WORDS` + `detect_signal()` 是请求路径的
关键词预分类闸门——只有命中「不是/不对/错了…」才异步调 LLM 验证；漏检=纠正永不处理，
且违反项目红线（路由/意图判断必须 agentic，feedback_no_hardcoded_classifiers.md）。
用户明确要求：删除关键词闸门，每条消息无条件走 LLM 判定（成本暂不考虑）。

本文件按 A4.8 先行：实现前运行必须 RED。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_memory_clarification_agentic.py -v
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.services.memory_clarification_service as mcl  # noqa: E402


class _Res:
    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _DB:
    def __init__(self, rows=None):
        self.sqls = []
        self.added = []
        self.commits = 0
        self.events = []  # 有序事件：("add", TypeName) / ("commit", "")
        self._rows = rows or []
        self.gets = []  # ("ModelName", id)
        self.deleted = []
        # ORM 化 negate/forget 分支（余留清零波 2026-10-02）的目标概念桩：
        # 行为级断言直接核对属性变更，不再断言 SQL 字符串（实现细节）。
        self.concept = SimpleNamespace(
            id="c1", user_id="u1", valid_to=None, weight=1.0, status="active",
            updated_at=None, description_full="", description_short="",
            metadata_json=None, activation_strength=0.5, needs_review=False,
            source_trust="user_stated",
        )

    async def execute(self, stmt, params=None):
        self.sqls.append(str(stmt))
        return _Res(rows=self._rows)

    async def get(self, model, ident):
        self.gets.append((model.__name__, ident))
        if model.__name__ == "MemoryConcept":
            return self.concept
        return None

    async def delete(self, obj):
        self.deleted.append(obj)

    def begin_nested(self):
        return _Nested()

    def add(self, obj):
        self.added.append(obj)
        self.events.append(("add", type(obj).__name__))

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1
        self.events.append(("commit", ""))


def _committed_after(db, event) -> bool:
    """账目行必须在记录之后 commit（否则随会话回滚——本波修复的正是这个失败模式）。"""
    try:
        idx = db.events.index(event)
    except ValueError:
        return False
    return any(e[0] == "commit" for e in db.events[idx + 1:])


def _fake_llm(payload: str):
    class _L:
        async def complete_chat(self, msgs, **kw):
            return payload
    return _L()


def _run(coro):
    return asyncio.run(coro)


# ── 1: 关键词表与闸门函数彻底删除 ─────────────────────────────────────────

def test_signal_words_and_detect_signal_removed():
    assert not hasattr(mcl, "_SIGNAL_WORDS"), "关键词表必须删除（用户红线）"
    assert not hasattr(mcl, "detect_signal"), "关键词闸门函数必须删除"


# ── 2/3: 调用方不再有请求路径关键词闸门 ───────────────────────────────────

def test_chat_schedules_clarification_without_keyword_gate():
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "api", "chat.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "import detect_signal" not in src, "chat 请求路径不得再有关键词预分类"
    assert "detect_signal(" not in src, "chat 不得调用关键词闸门"
    assert "process_clarification(" in src, "必须仍调度异步 LLM 澄清检测"
    # A4.9 r1 Important-1：regenerate 是对旧轮的重新生成，不重复检测。
    assert "not request.regenerate_from_message_id" in src
    assert "user_message = None" in src, "regenerate 分支不得依赖未绑定的 user_message"


def test_voice_schedules_shadow_without_keyword_gate():
    src_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "services", "voice_service.py"
    )
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    assert "import detect_signal" not in src, "voice 路径不得再有关键词预分类"
    assert "detect_signal(" not in src, "voice 不得调用关键词闸门"
    assert "_clarification_shadow(" in src, "必须仍调度 shadow 检测"
    # A4.9 r1 Important-2/3：合成系统通知(user_turn=False)不检测；运行时开关必须遵守。
    assert "user_turn and text" in src
    assert "user_turn=False" in src
    assert "memory_runtime_enabled(get_config())" in src


# ── 4/5: LLM 判定是唯一闸门（无信号词的消息同样被处理）────────────────────

def test_non_correction_is_noop(monkeypatch):
    import app.services.memory_llm_factory as mlf
    monkeypatch.setattr(mlf, "_memory_llm", lambda kind: _fake_llm(
        '{"is_correction": false, "correction_type": "negate", '
        '"affected_concept_ids": [], "confidence": 0.1}'
    ))
    monkeypatch.setattr(mcl, "config", SimpleNamespace(memory={
        "clarification_temperature": 0.1,
        "clarification_auto_apply_threshold": 0.8,
    }))
    db = _DB()
    parsed = _run(mcl.process_clarification(db, "u1", "我最近在研究量化交易"))
    assert parsed is None
    clars = [o for o in db.added if type(o).__name__ == "MemoryClarification"]
    assert not clars, "非纠正不得写入澄清候选"
    assert not any("UPDATE memory_concepts" in s for s in db.sqls)
    # 2026-09-20：非纠正检测仍是真实 LLM 调用，账目必须落库（否则 agentic 后
    # 绝大多数调用丢失）——且按遥测口径记 read，不推高写路径降级梯子。
    calls = [o for o in db.added if type(o).__name__ == "MemoryLLMCall"]
    assert calls and calls[0].billing_class == "read"
    assert _committed_after(db, ("add", "MemoryLLMCall")), \
        "非纠正路径必须在记账之后 commit（否则账目随会话回滚）"


def test_parse_failure_still_records_telemetry(monkeypatch):
    """调用了 LLM 就产生成本：解析失败也必须落账（read 遥测），不得静默丢弃。"""
    import app.services.memory_llm_factory as mlf
    monkeypatch.setattr(mlf, "_memory_llm", lambda kind: _fake_llm("这不是 JSON"))
    monkeypatch.setattr(mcl, "config", SimpleNamespace(memory={
        "clarification_temperature": 0.1,
        "clarification_auto_apply_threshold": 0.8,
    }))
    db = _DB()
    parsed = _run(mcl.process_clarification(db, "u1", "随便说点什么"))
    assert parsed is None
    calls = [o for o in db.added if type(o).__name__ == "MemoryLLMCall"]
    assert calls and calls[0].billing_class == "read"
    assert _committed_after(db, ("add", "MemoryLLMCall")), \
        "解析失败路径也必须在记账之后 commit"


def test_low_confidence_correction_is_read_not_write(monkeypatch):
    """A4.9 r1 Minor-2：纠正但置信度不足未应用 → 不得计 write（不推高降级梯子）。"""
    import app.services.memory_llm_factory as mlf
    monkeypatch.setattr(mlf, "_memory_llm", lambda kind: _fake_llm(
        '{"is_correction": true, "correction_type": "negate", '
        '"affected_concept_ids": ["c1"], "new_description": "", "confidence": 0.5}'
    ))
    monkeypatch.setattr(mcl, "config", SimpleNamespace(memory={
        "clarification_temperature": 0.1,
        "clarification_auto_apply_threshold": 0.8,
    }))
    db = _DB()
    parsed = _run(mcl.process_clarification(db, "u1", "我最近在研究量化交易"))
    assert parsed and parsed["is_correction"]
    clars = [o for o in db.added if type(o).__name__ == "MemoryClarification"]
    assert clars and clars[0].applied is False
    assert not any("UPDATE memory_concepts" in s for s in db.sqls)
    calls = [o for o in db.added if type(o).__name__ == "MemoryLLMCall"]
    assert calls and calls[0].billing_class == "read"


def test_llm_judgment_applies_even_without_signal_words(monkeypatch):
    """无任何关键词的消息（旧闸门必然漏检）也必须由 LLM 判定并生效。"""
    import app.services.memory_llm_factory as mlf
    monkeypatch.setattr(mlf, "_memory_llm", lambda kind: _fake_llm(
        '{"is_correction": true, "correction_type": "negate", '
        '"affected_concept_ids": ["c1"], "new_description": "", "confidence": 0.95}'
    ))
    monkeypatch.setattr(mcl, "config", SimpleNamespace(memory={
        "clarification_temperature": 0.1,
        "clarification_auto_apply_threshold": 0.8,
    }))
    db = _DB()
    parsed = _run(mcl.process_clarification(db, "u1", "我最近在研究量化交易"))
    assert parsed and parsed["is_correction"]
    clars = [o for o in db.added if type(o).__name__ == "MemoryClarification"]
    assert clars and clars[0].applied is True
    assert db.concept.valid_to is not None and db.concept.weight == 0 \
        and db.concept.updated_at is not None, \
        "negate 必须实际生效（LLM 判定即唯一闸门）——ORM 化后按属性变更断言"
    calls = [o for o in db.added if type(o).__name__ == "MemoryLLMCall"]
    assert calls and calls[0].billing_class == "write", "真正写路径的纠正记 write"
