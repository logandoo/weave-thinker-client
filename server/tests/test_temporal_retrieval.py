"""时间轴检索（time-axis retrieval）单元测试 — 2026-09-11 wave-3.

Covers acceptance criteria #7-#11:
  T1 citation ledger keeps + renders published_date (model-visible view)
  T2 web_search tool declares time_sensitive and forwards it
  T3 every provider request carries its time filter when time_sensitive=True
  T4 system prompt carries the temporal-awareness rule
  T5 auditor template carries the temporal-conflict exit rule
  T6 current-time awareness (chat system prompt + voice per-query clock)

Run: cd backend && python -m pytest tests/test_temporal_retrieval.py -v
"""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_config

config = get_config()


# ── helpers ──────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = "fake"

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class RecordingClient:
    """httpx.AsyncClient stand-in that records the last POST body/headers."""

    def __init__(self, payload):
        self.payload = payload
        self.last_json = None
        self.last_headers = None

    async def post(self, url, **kw):
        self.last_json = kw.get("json")
        self.last_headers = kw.get("headers")
        return FakeResponse(self.payload)


# ── T1: ledger date passthrough ──────────────────────────────────────────
def test_t1_ledger_renders_published_date():
    from app.services.citation_ledger import CitationLedger

    ledger = CitationLedger()
    ids = ledger.register_hits([
        {"title": "旧来源", "url": "https://example.com/old", "snippet": "s1",
         "published_date": "2024-03-01"},
        {"title": "新来源", "url": "https://example.com/new", "snippet": "s2",
         "published_date": "2026-08-15"},
        {"title": "无日期", "url": "https://example.com/none", "snippet": "s3"},
    ])
    out = ledger.format_hits(ids)
    assert "发布日期: 2024-03-01" in out, out
    assert "发布日期: 2026-08-15" in out, out
    assert out.count("发布日期:") == 2, out
    assert ledger.entries[1]["published_date"] == "2026-08-15"
    assert ledger.entries[2]["published_date"] == ""
    # re-registering the same URL with a later known date fills the blank
    ledger.register("https://example.com/none", published_date="2026-09-01")
    assert ledger.entries[2]["published_date"] == "2026-09-01"


# ── T2: tool schema + handler passthrough ────────────────────────────────
def test_t2_web_search_tool_forwards_time_sensitive():
    from app.tools import web_search as ws_mod
    from app.tools.registry import registry

    schema = registry.get_schema("web_search")
    assert schema is not None, "web_search not registered"
    props = schema["parameters"]["properties"]
    assert "time_sensitive" in props, props
    assert props["time_sensitive"]["type"] == "boolean"

    captured = {}

    class FakeSearchService:
        def __init__(self):
            pass

        @property
        def is_available(self):
            return True

        async def search_multiple(self, queries, time_sensitive=False):
            captured["queries"] = queries
            captured["time_sensitive"] = time_sensitive
            return []

        async def persist_hits(self, hits, user_id=None, conversation_id=None):
            pass

        def format_hits(self, hits):
            return ""

    async def _call(args):
        with patch.object(ws_mod, "WebSearchService", FakeSearchService), \
             patch.object(ws_mod, "_fetch_dates_for_hits", AsyncMock()):
            return await ws_mod.web_search(args)

    asyncio.run(_call({"queries": ["测试时效"], "time_sensitive": True}))
    assert captured["time_sensitive"] is True, captured
    asyncio.run(_call({"queries": ["测试常识"]}))
    assert captured["time_sensitive"] is False, captured
    # strict string parsing: bool("false") is True — must not enable the filter
    asyncio.run(_call({"queries": ["测试字符串"], "time_sensitive": "false"}))
    assert captured["time_sensitive"] is False, captured
    asyncio.run(_call({"queries": ["测试字符串真"], "time_sensitive": "true"}))
    assert captured["time_sensitive"] is True, captured


# ── T3: provider-level time filters ──────────────────────────────────────
def test_t3_provider_time_params():
    from app.services.search_service import WebSearchService

    svc = WebSearchService()

    # Tavily: time_range=month + published_date parsed
    tavily_client = RecordingClient({
        "results": [{"title": "T", "url": "https://e.com/t", "content": "c",
                     "published_date": "2026-08-15T00:00:00Z"}],
    })
    hits = asyncio.run(svc._search_tavily(tavily_client, "q", time_sensitive=True))
    assert tavily_client.last_json.get("time_range") == "month", tavily_client.last_json
    assert hits[0].published_date == "2026-08-15", hits[0]
    asyncio.run(svc._search_tavily(tavily_client, "q", time_sensitive=False))
    assert "time_range" not in tavily_client.last_json, tavily_client.last_json

    # Serper: tbs=qdr:m + real-world date formats (English / relative)
    serper_client = RecordingClient({
        "organic": [
            {"title": "S", "link": "https://e.com/s", "snippet": "c",
             "date": "Aug 20, 2026"},
            {"title": "S2", "link": "https://e.com/s2", "snippet": "c",
             "date": "2 days ago"},
        ],
    })
    hits = asyncio.run(svc._search_serper(serper_client, "q", time_sensitive=True))
    assert serper_client.last_json.get("tbs") == "qdr:m", serper_client.last_json
    assert hits[0].published_date == "2026-08-20", hits[0]
    expected_rel = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    assert hits[1].published_date == expected_rel, hits[1]
    asyncio.run(svc._search_serper(serper_client, "q", time_sensitive=False))
    assert "tbs" not in serper_client.last_json, serper_client.last_json

    # Bocha: freshness oneMonth / noLimit
    bocha_client = RecordingClient({
        "code": 200,
        "data": {"webPages": {"value": [
            {"url": "https://e.com/b", "name": "B", "snippet": "c",
             "datePublished": "2026-08-10T00:00:00Z"},
        ]}},
    })
    with patch.dict(config.web_search, {"bocha_api_key": "test-key"}), \
         patch("app.services.search_service.get_shared_async_client",
               return_value=bocha_client):
        asyncio.run(svc._search_bocha("q", time_sensitive=True))
        assert bocha_client.last_json.get("freshness") == "oneMonth", bocha_client.last_json
        asyncio.run(svc._search_bocha("q", time_sensitive=False))
        assert bocha_client.last_json.get("freshness") == "noLimit", bocha_client.last_json

    # Firecrawl: tbs=qdr:m (and absent when not time-sensitive)
    fc_client = RecordingClient({
        "success": True,
        "data": {"web": [{"title": "F", "url": "https://e.com/f", "description": "c",
                          "date": "2026-08-21"}]},
    })
    with patch.dict(config.web_search, {"firecrawl_api_key": "test-key"}), \
         patch("app.services.search_service.get_shared_async_client",
               return_value=fc_client):
        hits = asyncio.run(svc._search_firecrawl("q", time_sensitive=True))
        assert fc_client.last_json.get("tbs") == "qdr:m", fc_client.last_json
        assert hits[0].published_date == "2026-08-21", hits[0]
        asyncio.run(svc._search_firecrawl("q", time_sensitive=False))
        assert "tbs" not in fc_client.last_json, fc_client.last_json

    # Exa: startPublishedDate only when time_sensitive
    exa_mock = AsyncMock(return_value=(
        "Title: E\nURL: https://e.com/e\nPublished: 2026-08-01\n"
        "snippet long enough to be captured as a snippet line"
    ))
    with patch("app.tools.mcp_client.call_mcp_tool", exa_mock):
        asyncio.run(svc._search_exa("q", time_sensitive=True))
        args_true = exa_mock.await_args.args[2]
        assert args_true.get("startPublishedDate", "").startswith(str(datetime.now().year)), args_true
        exa_mock.reset_mock()
        asyncio.run(svc._search_exa("q", time_sensitive=False))
        args_false = exa_mock.await_args.args[2]
        assert "startPublishedDate" not in args_false, args_false


# ── T3b: publish-date normalisation keeps two-digit days intact ──────────
def test_t3b_normalise_date_two_digit_days():
    from app.services.search_service import _normalise_date

    # RED-watched regression: ordered alternation used to truncate day 15→01,
    # 31→03 — publish dates feed recency ordering, so this corrupted timelines.
    assert _normalise_date("2026-08-15") == "2026-08-15"
    assert _normalise_date("2026-08-15T00:00:00Z") == "2026-08-15"
    assert _normalise_date("2025-12-31") == "2025-12-31"
    assert _normalise_date("2026-10-09") == "2026-10-09"
    assert _normalise_date("2026-8-5") == "2026-08-05"
    assert _normalise_date("2026/08/15") == "2026-08-15"
    assert _normalise_date("garbage") is None
    # English month (Serper real format) + relative (Serper real format)
    assert _normalise_date("Aug 15, 2026") == "2026-08-15"
    assert _normalise_date("August 5, 2026") == "2026-08-05"
    expected_rel = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    assert _normalise_date("2 days ago") == expected_rel
    # Malformed days must be rejected, not prefix-matched into bogus dates
    assert _normalise_date("2026-08-32") is None
    assert _normalise_date("2026-08-155") is None


# ── T4: writer-side system prompt rule ───────────────────────────────────
def test_t4_system_prompt_temporal_rule():
    from app.services import agent_service as as_mod
    from app.services.agent_service import AgentService

    rule = as_mod._TEMPORAL_AWARENESS_RULE
    assert "发布日期" in rule and "最新" in rule, rule
    assert "time_sensitive=true" in rule, rule
    src = inspect.getsource(AgentService._build_system_prompt)
    assert "_TEMPORAL_AWARENESS_RULE" in src, "rule not wired into build_system_prompt"


# ── T5: auditor temporal-conflict exit ───────────────────────────────────
def test_t5_auditor_temporal_conflict_rule():
    from app.services.agent_loop import AgentLoop, _AUDIT_TEMPORAL_CONFLICT_RULE

    assert "最新" in _AUDIT_TEMPORAL_CONFLICT_RULE, _AUDIT_TEMPORAL_CONFLICT_RULE
    assert "needs_evidence" in _AUDIT_TEMPORAL_CONFLICT_RULE, _AUDIT_TEMPORAL_CONFLICT_RULE
    assert "发布日期" in _AUDIT_TEMPORAL_CONFLICT_RULE, _AUDIT_TEMPORAL_CONFLICT_RULE
    assert _AUDIT_TEMPORAL_CONFLICT_RULE in AgentLoop._AUDITOR_SYSTEM_TEMPLATE, (
        "temporal rule not embedded in the always-on auditor template"
    )


# ── T6: current-time awareness ───────────────────────────────────────────
def test_t6_current_time_awareness():
    from app.services import agent_service as as_mod
    from app.services.agent_service import AgentService
    from app.services.voice_service import _local_time_line, _stamp_query_time

    src = inspect.getsource(AgentService._build_system_prompt)
    assert "当前时间" in src and "北京时间" in src, "chat system prompt lost its clock line"
    line = _local_time_line()
    today_cn = datetime.now(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日")
    assert "当前时间" in line and "北京时间" in line, line
    assert today_cn in line, line
    # Voice: every turn's last user message is stamped with the local clock,
    # via a NEW dict slot (the shared history dict must not be mutated).
    first = {"role": "user", "content": "现在几点"}
    original = [first]
    stamped = _stamp_query_time(original)
    assert "当前时间" in stamped[-1]["content"] and "北京时间" in stamped[-1]["content"], stamped
    assert first["content"] == "现在几点", "original history dict was mutated in place"
    assert stamped[-1] is not first, "stamped slot must be a new dict"
