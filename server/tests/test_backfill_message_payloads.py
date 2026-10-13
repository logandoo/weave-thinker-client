"""TDD tests for backfill process_row (Task 3).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_backfill_message_payloads.py -v
"""
import json

from app.services.message_payload_service import build_field_stub, compute_sha256
from scripts.backfill_message_payloads import process_row

MAX = 1000


def _run(field, value, existing=None):
    return process_row(field, value, existing if existing is not None else set(),
                       MAX, build_field_stub, compute_sha256)


class TestProcessRow:
    def test_thin_skipped(self):
        v, p, s = _run("tool_results", "x" * 100)
        assert s == "skipped_thin" and p is None and v == "x" * 100

    def test_empty_none_skipped(self):
        assert _run("tool_results", "")[2] == "skipped_thin"
        assert _run("tool_results", None)[2] == "skipped_thin"

    def test_fat_externalized(self):
        fat = "r" * 5000
        v, p, s = _run("reasoning_content", fat)
        assert s == "externalized"
        assert p[0] == "reasoning_content" and p[1] == fat and p[2] == 5000
        assert p[3] == compute_sha256(fat)
        assert json.loads(v)["_externalized"] is True

    def test_stub_idempotent(self):
        fat = "r" * 5000
        stub, _, _ = _run("reasoning_content", fat)
        v, p, s = _run("reasoning_content", stub)
        assert s == "skipped_stub" and p is None and v == stub

    def test_sha_dup_still_gets_own_row(self):
        """W3 项2（2026-10-06）：跨消息 sha 去重废除——重复内容也写**本消息**行。

        旧 skipped_dup 契约（p1=None）让调用方写出的 stub.payload_ref 指向他消息
        的 payload 行；路由按 (message_id, field) 归属寻址后这些消息永久 404
        孤儿（前端「全文取回失败」重试永不成功）。"""
        fat = "r" * 5000
        sha = compute_sha256(fat)
        v1, p1, s1 = _run("reasoning_content", fat, {sha})
        assert s1 == "duplicated" and p1 is not None, "重复内容必须产出本消息 payload 行"
        assert p1[1] == fat  # 本消息自己的行内容（可被归属寻址取回）
        assert p1[3] == sha
        assert json.loads(v1)["_externalized"] is True
