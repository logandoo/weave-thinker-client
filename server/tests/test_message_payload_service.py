"""TDD tests for message payload externalization pure functions (Task 1).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_message_payload_service.py -v
"""
import json

import pytest

from app.services.message_payload_service import (
    INNER_PREVIEW_CHARS,
    INNER_TRUNCATE_CHARS,
    build_field_stub,
    compute_sha256,
    is_externalized_stub,
    slim_tool_results_json,
)


class TestSlimToolResultsJson:
    def test_short_strings_untouched(self):
        obj = {"a": "hello", "b": ["x", "y"], "c": {"d": "world"}}
        assert slim_tool_results_json(obj) == obj

    def test_long_string_truncated_with_markers(self):
        long = "x" * (INNER_TRUNCATE_CHARS + 1000)
        out = slim_tool_results_json({"content": long})
        assert len(out["content"]) == INNER_PREVIEW_CHARS
        assert out["content"] == long[:INNER_PREVIEW_CHARS]
        assert out["__truncated__"] is True
        assert out["__size_bytes__"] == len(long)

    def test_nested_structures_truncated(self):
        # 2026-10-03 契约变更（用户红线：回答正文永不静默截断）：原 fixture 用
        # type:"text"（=正文段）断言 [:500] 切片，编码的正是 conv 8c03ff8e 事故行为。
        # 深层遍历截断的覆盖意图保留——fixture 改 type:"tool"；正文豁免契约见
        # tests/test_body_exempt_slim.py。
        long = "中" * (INNER_TRUNCATE_CHARS * 2)
        obj = {"display_sequence": [{"type": "tool", "content": long}]}
        out = slim_tool_results_json(obj)
        item = out["display_sequence"][0]
        assert len(item["content"]) == INNER_PREVIEW_CHARS
        assert item["__truncated__"] is True
        assert item["__size_bytes__"] == len(long)

    def test_nested_text_body_exempt(self):
        """display_sequence text 段（回答正文）豁免预览切片——conv 8c03ff8e 回归钉。"""
        long = "中" * (INNER_TRUNCATE_CHARS * 2)
        obj = {"display_sequence": [{"type": "text", "content": long}]}
        out = slim_tool_results_json(obj)
        item = out["display_sequence"][0]
        assert len(item["content"]) == len(long)
        assert "__truncated__" not in item

    def test_structure_preserved_for_rendering(self):
        """display_sequence 形（type/title/status 等渲染键）必须保留，前端才可渲染工具卡。"""
        obj = {
            "rounds": [{"round": 1, "queries": ["q1"]}],
            "results": [{"id": 1, "title": "t", "url": "u", "snippet": "s" * 50}],
            "display_sequence": [
                {"type": "tool", "name": "call_1", "title": "工具调用 · workspace_read",
                 "status": "completed", "result": "r" * (INNER_TRUNCATE_CHARS * 3)},
                {"type": "reasoning_step", "title": "思考过程", "content": "短"},
            ],
        }
        out = slim_tool_results_json(obj)
        ds = out["display_sequence"]
        assert ds[0]["type"] == "tool"
        assert ds[0]["title"] == "工具调用 · workspace_read"
        assert ds[0]["status"] == "completed"
        assert ds[0]["__truncated__"] is True
        assert ds[1]["content"] == "短"
        assert "__truncated__" not in ds[1]
        assert out["results"][0]["id"] == 1

    def test_non_dict_input_passthrough(self):
        assert slim_tool_results_json(["a"]) == ["a"]
        assert slim_tool_results_json(None) is None


class TestBuildFieldStub:
    def test_sha256_stable(self):
        assert compute_sha256("abc") == compute_sha256("abc")
        assert compute_sha256("abc") != compute_sha256("abd")
        assert len(compute_sha256("abc")) == 64

    def test_stub_is_json_with_meta(self):
        stub = build_field_stub("reasoning_content", "y" * 50000)
        obj = json.loads(stub)
        assert obj["_externalized"] is True
        assert obj["field"] == "reasoning_content"
        assert obj["size_bytes"] == 50000
        assert obj["payload_ref"] == compute_sha256("y" * 50000)
        assert len(obj["preview"]) <= 2000

    def test_tool_results_stub_keeps_structure(self):
        raw = json.dumps({"display_sequence": [{"type": "tool", "title": "t", "content": "z" * 100000}]}, ensure_ascii=False)
        stub = build_field_stub("tool_results", raw)
        obj = json.loads(stub)
        assert obj["_externalized"] is True
        assert obj["field"] == "tool_results"
        assert obj["display_sequence"][0]["type"] == "tool"
        assert obj["display_sequence"][0]["__truncated__"] is True

    def test_is_externalized_stub_roundtrip(self):
        stub = build_field_stub("tool_calls", "c" * 40000)
        assert is_externalized_stub(stub) is True
        assert is_externalized_stub("plain text") is False
        assert is_externalized_stub('{"other": 1}') is False
        assert is_externalized_stub("") is False
        assert is_externalized_stub(None) is False


class TestMarkerSourceKeyHygiene:
    """UPSTREAM_TODO_20261006 项 12（评审 A m5）：标记仅由后端签发——
    内容自带 __size_bytes__/__truncated__ 源键不得抬/压徽标（A4.9 M5 整值覆盖契约）。"""

    def test_forged_marker_source_key_cannot_inflate_marker(self):
        long = "x" * (INNER_TRUNCATE_CHARS + 1000)
        out = slim_tool_results_json({"content": long, "__size_bytes__": 10**9})
        assert out["__truncated__"] is True
        assert out["__size_bytes__"] == len(long), (
            f"伪造源键抬标记：got {out['__size_bytes__']}，期望内容实长 {len(long)}"
        )

    def test_cut_dict_marker_is_whole_text_total(self):
        """整值覆盖口径=Σ内容键（与 _item_text_total/前端 _textTotal 同构，跨取回恒稳）。"""
        long = "x" * (INNER_TRUNCATE_CHARS + 1000)
        out = slim_tool_results_json({"a": long, "b": "short"})
        assert out["__truncated__"] is True
        assert out["__size_bytes__"] == len(long) + len("short"), (
            f"got {out['__size_bytes__']}，期望 Σ内容键 {len(long) + len('short')}"
        )

    def test_noncut_dict_strips_forged_markers(self):
        out = slim_tool_results_json({"content": "short", "__size_bytes__": 10**9,
                                      "__truncated__": True})
        assert "__size_bytes__" not in out, "非 cut 字典不得透传伪造标记"
        assert "__truncated__" not in out, "非 cut 字典不得透传伪造标记"

    def test_seq_item_noncut_strips_forged_markers(self):
        obj = {"display_sequence": [{"type": "tool", "name": "t", "title": "T",
                                     "content": "short", "__size_bytes__": 10**9,
                                     "__truncated__": True}]}
        item = slim_tool_results_json(obj)["display_sequence"][0]
        assert "__size_bytes__" not in item, "seq 项非 cut 不得透传伪造标记"
        assert "__truncated__" not in item


class TestStubShapeStrictness:
    """评审 A Critical-1（UPSTREAM_TODO 波 fix-round 1）：**标记信任**只信后端签发
    meta（payload_ref 64hex + field + size_bytes）——内容伪造 `_externalized`+标记
    不得绕过源键剥除；解析识别（is_externalized_stub）保持宽松形（路由契约既有钉）。"""

    @staticmethod
    def _legit_stub():
        return json.dumps({"_externalized": True, "field": "tool_results",
                           "payload_ref": "a" * 64, "size_bytes": 12,
                           "size_chars": 12, "preview": "x"})

    def test_legit_stub_markers_trusted(self):
        from app.services.message_payload_service import stub_markers_trusted, is_externalized_stub
        assert is_externalized_stub(self._legit_stub()) is True
        assert stub_markers_trusted(self._legit_stub()) is True

    def test_naive_forge_markers_untrusted(self):
        from app.services.message_payload_service import stub_markers_trusted, is_externalized_stub
        forged = json.dumps({"_externalized": True, "display_sequence": [
            {"type": "text", "content": "c", "__size_bytes__": 10**9, "__truncated__": True}]})
        assert is_externalized_stub(forged) is True, "解析识别保持宽松（伪造=404 安全）"
        assert stub_markers_trusted(forged) is False, "无 meta 的桩形内容标记不得信任"

    def test_forge_missing_payload_ref_untrusted(self):
        from app.services.message_payload_service import stub_markers_trusted
        forged = json.dumps({"_externalized": True, "field": "tool_results",
                             "size_bytes": 1, "items": [{"__size_bytes__": 10**9}]})
        assert stub_markers_trusted(forged) is False

    def test_forge_bad_payload_ref_untrusted(self):
        from app.services.message_payload_service import stub_markers_trusted
        forged = json.dumps({"_externalized": True, "field": "tool_results",
                             "payload_ref": "not-hex", "size_bytes": 1, "items": []})
        assert stub_markers_trusted(forged) is False

    def test_forge_bad_size_bytes_untrusted(self):
        from app.services.message_payload_service import stub_markers_trusted
        forged = json.dumps({"_externalized": True, "field": "tool_results",
                             "payload_ref": "b" * 64, "size_bytes": "big", "items": []})
        assert stub_markers_trusted(forged) is False

    def test_slim_message_dict_strips_forged_stub_markers(self):
        """复审 round2 New-#1：单字段伪造宽松桩的标记剥除必须真生效（changed 置位，
        不得因 identity 快路径把未剥字典原样返回）。"""
        from app.api.message_payloads import slim_message_dict
        forged = json.dumps({"_externalized": True, "payload_ref": "d" * 64,
                             "display_sequence": [{"type": "text", "content": "c",
                                                   "__size_bytes__": 10**9,
                                                   "__truncated__": True}]})
        out = slim_message_dict({"tool_results": forged})
        assert out is not None
        assert out.get("tool_results") != forged, "单字段伪造桩不得原样透传（剥除须落盘）"

        def _no_markers(v):
            if isinstance(v, list):
                return all(_no_markers(x) for x in v)
            if isinstance(v, dict):
                return all(k not in ("__size_bytes__", "__truncated__") and _no_markers(x)
                           for k, x in v.items())
            return True

        parsed = json.loads(out["tool_results"])
        assert _no_markers(parsed), f"伪造标记须剥除，got {parsed}"
        assert parsed.get("payload_ref") == "d" * 64, "解析键（payload_ref）须保留"

    def test_forge_nan_size_bytes_untrusted(self):
        """mini 复审 Minor-2：NaN/Infinity 的 size_bytes 不得信（与前端 isFinite 同判）。"""
        from app.services.message_payload_service import stub_markers_trusted
        for bad in ('NaN', 'Infinity'):
            forged = ('{"_externalized": true, "field": "tool_results", '
                      f'"payload_ref": "{"e" * 64}", "size_bytes": {bad}, "items": []}}')
            assert stub_markers_trusted(forged) is False, f"size_bytes={bad} 必须未信"

    def test_forge_huge_int_size_bytes_untrusted(self):
        """复审 B Important：巨整数 size_bytes 不得信更不得抛（isfinite 溢出=500 面）。"""
        from app.services.message_payload_service import stub_markers_trusted
        forged = ('{"_externalized": true, "field": "tool_results", '
                  f'"payload_ref": "{"e" * 64}", "size_bytes": {"9" * 400}, "items": []}}')
        try:
            ok = stub_markers_trusted(forged)
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"巨整数 size_bytes 必须拒绝而非抛出: {exc!r}")
        assert ok is False
