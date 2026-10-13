"""Web search fallback chain + web_search_results persistence test.

Run (project venv, PostgreSQL + backend running or DB reachable):
    source .venv/bin/activate
    python backend/tests/test_web_search_fallback.py

Covers:
  1. Provider chain: exa first, no duckduckgo anywhere.
  2. _provider_ready('exa') is keyless (True with empty exa_api_key).
  3. Exa MCP failure -> falls through to next configured provider (bocha).
  4. Hits carry provider/query attribution (persist_hits reads them).
  5. persist_hits opens its OWN session: rows written (query/provider/rank/
     title/url/snippet/published_date); multi-query persists serially.
  6. Empty hits: no-op without crash.
  7. persist_hits timeout / commit failure: swallowed, never raises (I4);
     caller's session is never touched (own AsyncSessionLocal).
"""
import asyncio
import os
import sys
import traceback
from unittest.mock import AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.core.config import get_config
from app.services.search_service import WebSearchService
from app.db.database import AsyncSessionLocal, WebSearchResult

config = get_config()

LOG_PATH = os.path.join(ROOT, "tests", "web_search_fallback.log")


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "fake"

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response=None, payload_builder=None):
        self._response = response
        self._payload_builder = payload_builder

    async def post(self, *a, **kw):
        if self._payload_builder is not None:
            query = (kw.get("json") or {}).get("query", "")
            return FakeResponse(200, self._payload_builder(query))
        return self._response


def bocha_payload(marker):
    return {
        "code": 200,
        "data": {
            "webPages": {
                "value": [
                    {
                        "url": f"https://example.com/{marker}-1",
                        "name": f"{marker} 标题一",
                        "snippet": f"{marker} 摘要一。",
                        "datePublished": "2026-01-05T00:00:00Z",
                    },
                    {
                        "url": f"https://example.com/{marker}-2",
                        "name": f"{marker} 标题二",
                        "snippet": f"{marker} 摘要二。",
                    },
                ]
            }
        },
    }


def per_query_payload(query):
    return bocha_payload(query)


async def run_checks(log):
    svc = WebSearchService()

    def line(s):
        print(s)
        log.write(s + "\n")

    # ---- 1. chain order: exa first, no duckduckgo ----
    chain = svc._provider_chain()
    assert chain[0] == "exa", f"exa must be primary, got {chain}"
    assert "duckduckgo" not in chain, f"duckduckgo must be removed, got {chain}"
    line(f"PASS chain = {chain}")

    # ---- 2. exa ready keyless ----
    assert svc._provider_ready("exa") is True, "_provider_ready('exa') must be True without a key"
    line("PASS _provider_ready('exa') is True with empty exa_api_key (keyless)")

    async with AsyncSessionLocal() as db:
        from sqlalchemy import select, text

        await db.execute(text("DELETE FROM web_search_results WHERE query LIKE '测试%'"))
        await db.commit()

        from app.db.database import WebSearchResult as WSR

        exa_down = patch("app.tools.mcp_client.call_mcp_tool",
                         AsyncMock(side_effect=RuntimeError("exa mcp down")))
        fake_http = patch("app.services.search_service.get_shared_async_client",
                          return_value=FakeClient(payload_builder=per_query_payload))
        only_exa_bocha = patch.object(WebSearchService, "_provider_ready",
                                      side_effect=lambda p: p in ("exa", "bocha"))

        # ---- 3+4. exa MCP fails -> bocha succeeds; hits carry attribution ----
        with patch.dict(config.web_search, {"bocha_api_key": "test-key"}), \
             exa_down, fake_http, only_exa_bocha:
            hits = await svc.search_multiple(["测试exa失败回退"])

        assert hits, "bocha fallback must return hits after exa failure"
        assert hits[0].title == "测试exa失败回退 标题一", hits[0].title
        assert hits[0].provider == "bocha" and hits[0].query == "测试exa失败回退", (
            hits[0].provider, hits[0].query)
        line(f"PASS exa fail -> bocha returned {len(hits)} hits with provider/query attribution")

        # ---- 5. persist_hits: own session, serial after gather ----
        with patch.dict(config.web_search, {"bocha_api_key": "test-key"}), \
             exa_down, fake_http, only_exa_bocha:
            multi = await svc.search_multiple(["测试query甲", "测试query乙"])
        assert len(multi) >= 2, f"multi-query must return hits, got {len(multi)}"
        await svc.persist_hits(multi)
        await svc.persist_hits(hits)

        result = await db.execute(
            select(WSR).where(WSR.query.like("测试%")).order_by(WSR.created_at, WSR.result_rank)
        )
        rows = result.scalars().all()
        by_query = {}
        for r in rows:
            by_query.setdefault(r.query, []).append(r)
        assert "测试query甲" in by_query and "测试query乙" in by_query, sorted(by_query)
        assert len(by_query["测试query甲"]) == 2 and len(by_query["测试query乙"]) == 2
        for r in rows:
            assert r.provider == "bocha", r.provider
            assert r.url.startswith("https://example.com/"), r.url
        qa = by_query["测试query甲"]
        assert [r.result_rank for r in qa] == [1, 2], [r.result_rank for r in qa]
        assert qa[0].published_date == "2026-01-05", qa[0].published_date
        line(f"PASS persisted {len(rows)} rows across 3 queries (own session, provider=bocha, "
             f"per-query rank 1/2, published_date kept; serial commit after gather)")

    # ---- 6. empty hits: no-op without crash ----
    await svc.persist_hits([])
    line("PASS empty-hits persistence is a no-op without crash")

    # ---- 7. persist_hits timeout / failure: swallowed, never raises (I4) ----
    class HungSession:
        def __init__(self):
            self.closed = False

        def add(self, *a, **kw):
            pass

        async def commit(self):
            await asyncio.sleep(60)

        async def close(self):
            self.closed = True

    class FailingSession(HungSession):
        async def commit(self):
            raise RuntimeError("commit exploded")

    class SessionCM:
        def __init__(self, session):
            self._session = session

        async def __aenter__(self):
            return self._session

        async def __aexit__(self, *a):
            await self._session.close()
            return False

    hung = SessionCM(HungSession())
    with patch.dict(config.web_search, {"timeout_seconds": 1.0}), \
         patch("app.db.database.AsyncSessionLocal", lambda: hung):
        await svc.persist_hits(hits)
    assert hung._session.closed, "timeout path must close the session (__aexit__)"
    line("PASS persist_hits commit-hang -> wait_for timeout -> swallowed, session closed, no raise")

    failing = SessionCM(FailingSession())
    with patch("app.db.database.AsyncSessionLocal", lambda: failing):
        await svc.persist_hits(hits)
    assert failing._session.closed, "exception path must close the session (__aexit__)"
    line("PASS persist_hits commit-exception -> swallowed, session closed, no raise")

    line("ALL CHECKS PASSED")


def main():
    os.makedirs(os.path.join(ROOT, "tests"), exist_ok=True)
    with open(LOG_PATH, "w") as log:
        try:
            asyncio.run(run_checks(log))
        except Exception:
            log.write("FAILED:\n" + traceback.format_exc())
            print(traceback.format_exc())
            sys.exit(1)


if __name__ == "__main__":
    main()
