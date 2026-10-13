"""Fake MCP stdio server for tests — newline-delimited JSON-RPC on stdin/stdout.

Implements the minimal MCP 2024-11-05 lifecycle: initialize,
notifications/initialized, tools/list, tools/call(echo).
"""
import json
import os
import sys


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    # NOTE: `for line in sys.stdin` uses block read-ahead on pipes — lines are
    # not delivered until EOF. readline() is line-buffered and prompt.
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        rid = req.get("id")
        if method == "initialize":
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-stdio", "version": "1.0"},
                },
            })
        elif method == "tools/list":
            send({
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo a message back",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            },
                        }
                    ]
                },
            })
        elif method == "tools/call":
            name = (req.get("params") or {}).get("name", "")
            args = (req.get("params") or {}).get("arguments") or {}
            if name == "echo":
                send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({
                                    "text": "echo:" + str(args.get("message", "")),
                                    "pid": os.getpid(),
                                }),
                            }
                        ],
                        "isError": False,
                    },
                })
            else:
                send({
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "unknown tool: " + name},
                })
        elif rid is not None:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()
