from app.tools.registry import ToolRegistry, ToolEntry, tool_error, registry as _registry

_imported = False


def _discover_tools():
    global _imported
    if _imported:
        return
    _imported = True

    import importlib
    import logging
    import pkgutil

    logger = logging.getLogger(__name__)

    # pkgutil.iter_modules：开发模式扫包目录；PyInstaller 冻结模式经
    # pyi_rth_pkgutil 运行时钩子（由本模块 import pkgutil 触发收集进包）注册到
    # pkgutil 的迭代协议——不依赖 .py 落盘。
    # 旧的 Path(__file__).glob("*.py") 在冻结下发现为空（安装版 tools 完全缺失根因）。
    for info in sorted(pkgutil.iter_modules(__path__, __name__ + "."), key=lambda i: i.name):
        if info.name in (f"{__name__}.__init__", f"{__name__}.registry"):
            continue
        try:
            importlib.import_module(info.name)
            logger.debug("Discovered tools from %s", info.name)
        except Exception as e:
            logger.warning("Could not import tool module %s: %s", info.name, e)


_discover_tools()

# Load MCP servers from config
try:
    from app.tools.mcp_client import load_mcp_servers_from_config
    load_mcp_servers_from_config()
except Exception:
    pass

registry = _registry
__all__ = ["registry", "ToolRegistry", "ToolEntry", "tool_error"]
