"""remote_call 执行上下文（PLAN_device_executor §2.1，主方案 §8.4）。

设备 executor 为云端驱动的工具调用建立 ``remote_call_scope()``。范围内：
- ``super_admin_bypass`` 强制失效（containment/静态校验一律执行）；
- ``_permission_granted`` 入站哨兵强制失效（权限门一律执行）。

本地 UI 主路径不受影响（ContextVar 缺省 False）。ContextVar（PEP 567/555）
而非 kwargs 穿线：包含性检查点深埋于 terminal/git/process/code_execution
内部 3-5 层调用之下，kwargs 穿线漏一处即 fail-open；contextvar 随 asyncio
task 传播，天然任务隔离。
"""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Iterator

_remote_call: ContextVar[bool] = ContextVar("remote_call", default=False)


def is_remote_call() -> bool:
    """当前执行是否处于设备 executor 的 remote_call 范围内。"""
    return _remote_call.get()


@contextmanager
def remote_call_scope() -> Iterator[None]:
    """进入 remote_call 严格模式（executor 的 tools/call 分发全程包裹）。"""
    token = _remote_call.set(True)
    try:
        yield
    finally:
        _remote_call.reset(token)


def super_admin_bypass_effective(cfg: Any = None) -> bool:
    """config.super_admin_bypass 的生效值：remote_call 下恒 False。

    ``cfg`` 可注入（缺省 get_config()）：调用方模块传自己的模块级 config 引用，
    保留 monkeypatch 注入接缝（如 test_tenant_namespace_p2 注入 terminal.config）。"""
    if _remote_call.get():
        return False
    from app.core.config import get_config

    c = cfg if cfg is not None else get_config()
    return bool(getattr(c, "super_admin_bypass", False))


def permission_sentinel_effective(args: Dict[str, Any]) -> bool:
    """``_permission_granted`` 哨兵的生效值：remote_call 下恒 False。"""
    if _remote_call.get():
        return False
    return bool((args or {}).get("_permission_granted"))
