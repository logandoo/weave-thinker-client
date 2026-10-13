"""Async-callable handler dispatch — dispatch() must await instances whose
__call__ is async (asyncio.iscoroutinefunction(instance) is False; the bound
__call__ must be inspected). Regression for the MCP handler class
(_MCPToolHandler) whose calls otherwise return an un-awaited coroutine.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.registry import ToolRegistry  # noqa: E402


class AsyncCallHandler:
    async def __call__(self, args, **kwargs):
        return "awaited-ok"


def test_async_call_instance_is_awaited():
    reg = ToolRegistry()
    reg.register(
        name="t_async_call",
        toolset="test",
        schema={"name": "t_async_call", "parameters": {"type": "object", "properties": {}}},
        handler=AsyncCallHandler(),
        is_async=True,
    )
    import asyncio
    result = asyncio.run(reg.dispatch("t_async_call", {}))
    assert result == "awaited-ok"


def test_plain_async_function_still_awaited():
    reg = ToolRegistry()
    async def handler(args, **kwargs):
        return "fn-ok"
    reg.register(
        name="t_async_fn",
        toolset="test",
        schema={"name": "t_async_fn", "parameters": {"type": "object", "properties": {}}},
        handler=handler,
        is_async=True,
    )
    import asyncio
    result = asyncio.run(reg.dispatch("t_async_fn", {}))
    assert result == "fn-ok"
