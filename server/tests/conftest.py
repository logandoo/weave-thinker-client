"""server/tests 共享接缝（W27 新增；上游无 conftest.py——本地自有，登记 §3）。

W20 已知类根治：测试用短命 `asyncio.run(...)` 事件循环跑真 DB，SQLAlchemy
async engine 连接池里的连接绑死在**已关闭**的循环上——后续测试复用即
`RuntimeError: Event loop is closed` / `attached to a different loop`，DB 写
链失败后按 fail-closed 设计被拒（实证：test_memory_retrieve_meta 的 real_db 腿
毒化 test_tma_p1b 的 live 腿「authorization audit unavailable」，全量序红、单跑绿）。

逐测排空连接池（`engine.sync_engine.pool.dispose()` 为同步调用，不依赖事件
循环）——下一测在自己的新循环里建全新连接。生产代码零改动。
"""
import pytest


@pytest.fixture(autouse=True)
def _drain_db_pool_between_tests():
    yield
    try:
        from app.db.database import engine

        engine.sync_engine.pool.dispose()
    except Exception:  # 无 DB 面的测试/引擎未初始化：无害
        pass
