"""main.py 设备执行器接线守卫（仿 test_main_sync_wiring；PLAN_device_executor §1.1 启动点）。

上游合并波覆盖 main.py 时极易静默吞掉本仓专属启动项（a29cce0 事故先例）——
两层钉死：行为层=client_exec 路由在 app 路由表；源码层=startup/shutdown 含
device_executor.start()/stop() 调用（逐字匹配，删行必红）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_main_device_executor_wiring.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _client_exec_route_paths() -> set:
    import main  # noqa: E402

    return {
        getattr(r, "path", "")
        for r in main.app.routes
        if getattr(r, "path", "").startswith("/api/client-exec")
    }


def test_client_exec_router_is_included():
    paths = _client_exec_route_paths()
    assert "/api/client-exec/status" in paths, "client_exec 路由缺失——main.py 被上游版覆盖"
    assert "/api/client-exec/pair" in paths
    assert "/api/client-exec/reset" in paths


def test_startup_starts_device_executor_worker():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "await device_executor.start()" in src, (
        "startup 缺 device_executor.start()——设备执行器 worker 不会启动"
    )


def test_shutdown_stops_device_executor_worker():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "await device_executor.stop()" in src, "shutdown 缺 device_executor.stop()"
