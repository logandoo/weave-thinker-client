"""Real-LLM behavioral probe (A4.7b evidence) — conv 0e8780e7 regression.

Runs the NEW coordinator template against the production failing scenario:
multi-turn context about 红魔 water-cooling phones, then the short follow-up
"那iqoo15ulra呢" (a very new phone model the model does not know).
Expected: search_required=True (the pre-fix template judged False — see
server log 2026-09-06 21:49:07 on <cloud-host>).

Usage: cd backend && python -m tests.probe_coordinator_new_entity
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.model_gateway.factory import build_llm_service
from app.model_gateway.registry import get_model_registry
from app.services.agent_loop import AgentLoop

MESSAGES = [
    {"role": "user", "content": "红魔11pro+和11spro+区别"},
    {"role": "assistant", "content": "红魔 11 Pro+ 是 2025 年 10 月发布的年度旗舰，全球首款量产水冷手机；红魔 11S Pro+ 是半代升级款……"},
    {"role": "user", "content": "水冷的意义大吗"},
    {"role": "assistant", "content": "水冷在手机上的意义关键在于解决风冷散热的物理瓶颈……"},
    {"role": "user", "content": "那iqoo15ulra呢"},
]


async def main() -> int:
    registry = get_model_registry()
    alias = sys.argv[1] if len(sys.argv) > 1 else "main"
    ep = registry.resolve(alias)
    llm = build_llm_service(ep)
    print(f"endpoint: alias={alias} model={ep.model_name} base={ep.base_url}")

    loop = AgentLoop.__new__(AgentLoop)
    loop.coordinator_llm = llm
    loop.deathmatch_manager = None
    loop._json_mode_disabled = set()
    loop.identity_name = "罗航"
    loop.assistant = None
    loop._identity_context = ""
    loop._last_coord_messages = []
    loop.provider_type = ep.provider_type
    loop.iteration_provider_type = ep.provider_type
    loop.iteration_extra_body_override = None
    loop.enable_reasoning = False
    loop.reasoning_effort = None
    loop.thinking_budget = None

    result = await loop._coordinate(MESSAGES)
    print(f"coordinator result: {result}")
    if not isinstance(result, dict):
        print("PROBE-FAIL: coordinator returned None")
        return 1
    sr = result.get("search_required")
    print(f"search_required={sr} focus={str(result.get('focus'))[:120]}")
    if sr is True:
        print("PROBE-PASS: new-entity query now judged search_required=True")
        return 0
    print("PROBE-FAIL: search_required is not True")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
