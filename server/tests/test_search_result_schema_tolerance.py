"""SearchHit schema-tolerance lock: provider/query fields must be inert
downstream (citation ledger + frontend-consumed shape).

Run: source .venv/bin/activate && python backend/tests/test_search_result_schema_tolerance.py
"""
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services.citation_ledger import build_ledger_from_tool_results

LOG_PATH = os.path.join(ROOT, "tests", "search_schema_tolerance.log")


def make_tool_result(extra_keys: bool) -> dict:
    results = [
        {
            "title": "FastAPI 官方文档",
            "url": "https://fastapi.tiangolo.com/",
            "snippet": "现代、快速的 Web 框架。",
            "published_date": "2026-01-05",
        },
        {
            "title": "Async SQLAlchemy",
            "url": "https://example.com/async-sqlalchemy",
            "snippet": "异步数据库访问指南。",
        },
    ]
    if extra_keys:
        for i, r in enumerate(results):
            r["provider"] = "exa"
            r["query"] = "fastapi 教程"
    return {
        "count": len(results),
        "results": results,
        "formatted": "formatted-text",
    }


def main():
    with open(LOG_PATH, "w") as log:
        try:
            def line(s):
                print(s)
                log.write(s + "\n")

            base = make_tool_result(extra_keys=False)
            extra = make_tool_result(extra_keys=True)

            ledger_base = build_ledger_from_tool_results(
                [{"name": "web_search", "result": json.dumps(base)}])
            ledger_extra = build_ledger_from_tool_results(
                [{"name": "web_search", "result": json.dumps(extra)}])

            assert ledger_base.entries == ledger_extra.entries, (
                ledger_base.entries, ledger_extra.entries)
            line("PASS citation ledger entries identical with/without provider/query keys")

            for hit in extra["results"]:
                assert hit["title"] and hit["url"] and "snippet" in hit
            line("PASS frontend-consumed keys (title/url/snippet) still present in tool JSON")

            formatted = ledger_base.format_hits([1, 2])
            assert "FastAPI 官方文档" in formatted and "fastapi.tiangolo.com" in formatted
            line("PASS ledger format_hits renders normally")

            line("ALL CHECKS PASSED")
        except Exception:
            log.write("FAILED:\n" + traceback.format_exc())
            print(traceback.format_exc())
            sys.exit(1)


if __name__ == "__main__":
    main()
