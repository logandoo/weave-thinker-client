"""main.py 同步接线守卫（2026-10-02 事故回归钉）。

事故：上游合并波 a29cce0 用上游 chatbot 纯服务端 main.py 覆盖本仓 main.py，
静默删掉 `sync_client.start()/stop()` 与 `sync_workspace_api` 路由——客户端
worker 从不启动 → resync_pending 恒真 → 前端「正在同步中」永挂（对话级诊断
docs/AUDIT_2026-10-02_sync_stall_and_arbitration_rejects.md §3）。

两层钉死（本仓专属启动项防合并再删）：
1. 行为层：/api/sync/workspace/* 路由必须在 app 路由表（include_router 实装）。
2. 源码层：startup/shutdown 必须含 sync_client.start()/stop() 调用（行为级
   等价物需拉起全量 lifespan——init_db/调度器簇过重，源码钉针对的恰是
   「合并删行」这一失效模式，逐字匹配即可）。

Run: cd server && ../.localstack/venv/bin/python -m pytest tests/test_main_sync_wiring.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _workspace_route_paths() -> set:
    import main  # noqa: E402

    return {
        getattr(r, "path", "")
        for r in main.app.routes
        if getattr(r, "path", "").startswith("/api/sync/workspace")
    }


def test_workspace_sync_router_is_included():
    paths = _workspace_route_paths()
    assert "/api/sync/workspace/status" in paths, (
        "sync_workspace 路由缺失——main.py 被上游版覆盖（a29cce0 同类事故）"
    )


def test_startup_starts_sync_client_worker():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "await sync_client.start()" in src, (
        "startup 缺 sync_client.start()——客户端同步 worker 不会启动（事故②根因）"
    )


def test_shutdown_stops_sync_client_worker():
    src = open(os.path.join(os.path.dirname(__file__), "..", "main.py")).read()
    assert "await sync_client.stop()" in src, "shutdown 缺 sync_client.stop()"


def test_client_specific_config_properties_survive_upstream_merges():
    """a29cce0 同类事故第二面：上游 config.py 覆盖删掉 12 个 client 专属 sync 属性
    （worker 起来也会在首周期 AttributeError；scratch 实测 startup 崩）。
    本测试钉死存在性——上游合并后必跑。"""
    import main  # noqa: F401  (确保 Config 模块已加载)

    from app.core.config import get_config

    cfg = get_config()
    for prop in (
        "sync_workspace_enabled", "sync_workspace_auto_max_mb", "sync_workspace_max_mb",
        "sync_workspace_pull_cap", "sync_workspace_pull_budget_seconds",
        "sync_client", "sync_client_enabled", "sync_client_server_url",
        "sync_client_username", "sync_client_password",
        "sync_client_interval", "sync_client_batch",
    ):
        assert hasattr(cfg, prop), f"Config 丢失 client 专属属性 {prop}（a29cce0 同类事故）"
