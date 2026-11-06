"""W6-1 模型分档（记忆/长程 v2，2026-10-23，判据 #26）：按 endpoint
context_window 自动选择 designed/raw/hybrid；D4=a 用户决策——≥128k 走 hybrid。

Run from backend/:  ../.venv/bin/python -m pytest tests/test_memory_tiering.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestSelectMemoryMode:
    def test_hybrid_at_128k(self):
        from app.services.memory_tiering_service import select_memory_mode

        assert select_memory_mode(131072) == "hybrid"
        assert select_memory_mode(200000) == "hybrid"

    def test_designed_below_threshold(self):
        from app.services.memory_tiering_service import select_memory_mode

        assert select_memory_mode(32768) == "designed"
        assert select_memory_mode(131071) == "designed"

    def test_explicit_tier_override(self):
        from app.services.memory_tiering_service import select_memory_mode

        assert select_memory_mode(8000, tier="raw") == "raw"
        assert select_memory_mode(200000, tier="designed") == "designed"

    def test_unknown_window_fallback(self):
        from app.services.memory_tiering_service import select_memory_mode

        assert select_memory_mode(0) == "designed"

    def test_raw_means_no_designed_injection(self):
        from app.services.memory_tiering_service import injection_enabled

        assert injection_enabled("hybrid") is True
        assert injection_enabled("designed") is True
        assert injection_enabled("raw") is False
