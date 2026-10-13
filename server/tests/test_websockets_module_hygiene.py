"""共享第三方模块卫生守卫（2026-10-10 污染事故回归钉）。

事故形态：测试用裸 setattr 替换 `websockets.connect`（假 monkeypatch 无 undo）
→ 假实现泄漏到共享模块 → 后续任何 `async with websockets.connect(...)` 的
测试被毒化（device executor 回路 12 腿全红实证；真因 test_asr_dictation_
keepalive._MP 假夹具，已修）。本守卫断言 connect 仍是官方实现本体。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_websockets_module_hygiene.py -v
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets  # noqa: E402
from websockets.asyncio import client as _ws_client  # noqa: E402


def test_websockets_connect_not_leaked():
    assert websockets.connect is _ws_client.connect, (
        "websockets.connect 被测试替换且未恢复——共享模块污染（找裸 setattr/mock 无 undo）"
    )


def test_websockets_serve_not_leaked():
    from websockets.asyncio import server as _ws_server

    assert websockets.serve is _ws_server.serve, (
        "websockets.serve 被测试替换且未恢复——共享模块污染"
    )
