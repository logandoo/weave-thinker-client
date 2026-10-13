"""TDD tests for slim/full message reads + payload endpoint (Task 4).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_message_payload_api.py -v
"""
import asyncio
import json
import os

from app.services.message_payload_service import (
    build_field_stub,
    is_externalized_stub,
)
from app.api.message_payloads import slim_message_dict


class TestSlimMessageDict:
    def test_thin_message_unchanged(self):
        m = {"id": "m1", "role": "user", "content": "hi", "reasoning_content": None,
             "tool_results": None, "tool_calls": None}
        out = slim_message_dict(m)
        assert out == m

    def test_fat_fields_replaced_by_stub_shape(self):
        m = {"id": "m2", "role": "assistant", "content": "ok",
             "reasoning_content": "r" * 100_000,
             "tool_results": json.dumps({"display_sequence": [{"type": "tool", "title": "t",
                                                               "content": "z" * 200_000}]}),
             "tool_calls": None}
        out = slim_message_dict(m)
        rc = json.loads(out["reasoning_content"])
        assert rc["_externalized"] is True
        assert rc["size_bytes"] == 100_000
        assert "preview" in rc
        tr = json.loads(out["tool_results"])
        assert tr["_externalized"] is True
        assert tr["display_sequence"][0]["type"] == "tool"
        assert tr["display_sequence"][0]["__truncated__"] is True
        # 原文不可再出现在瘦载荷里
        assert "z" * 2000 not in out["tool_results"]

    def test_already_stub_untouched(self):
        stub = build_field_stub("reasoning_content", "x" * 50_000)
        m = {"id": "m3", "reasoning_content": stub}
        out = slim_message_dict(m)
        assert out["reasoning_content"] == stub

    def test_non_dict_passthrough(self):
        assert slim_message_dict(None) is None


class TestSlimLazyList:
    """懒加载回归（用户实测「全部都会加载」，2026-10-06 二波）：slim 列表读
    出桩条件=截断即出桩（D-11 修订）——超预览容量/含被切串的字段必须出桩，
    thin 全量=展示基线原样。"""

    def test_slim_stubs_cuttable_under_cap_field(self):
        # 5k 字思考：低于 payload_inline_max_chars(32k) 但超 preview(2000)——
        # 旧实现整包下发（回归主因），必须出桩
        m = {"reasoning_content": "思" * 5000}
        out = slim_message_dict(m)
        assert is_externalized_stub(out["reasoning_content"]), "超预览容量字段必须出桩"
        assert "思" * 5000 not in out["reasoning_content"], "全文不得随列表下发"

    def test_slim_tool_results_cut_gets_stub(self):
        big = json.dumps({
            "display_sequence": [{"type": "tool", "name": "t", "title": "工具",
                                  "status": "completed", "content": "x" * 4000}]},
            ensure_ascii=False)
        out = slim_message_dict({"tool_results": big})
        assert is_externalized_stub(out["tool_results"]), "被切串的 tool_results 必须出桩"
        assert '"__truncated__"' in out["tool_results"]
        assert "x" * 4000 not in out["tool_results"]

    def test_slim_keeps_thin_field_plain(self):
        # thin 全量即展示基线（wire 等价不加桩）——test_thin_message_unchanged 语义保留
        out = slim_message_dict({"reasoning_content": "短思考", "content": "正文"})
        assert out["reasoning_content"] == "短思考"
        assert out["content"] == "正文"

    def test_messages_route_slim_no_resolve(self):
        src = open(
            os.path.join(os.path.dirname(__file__), "..", "app", "api", "conversation.py"),
            encoding="utf-8").read()
        fn_start = src.index("async def get_messages(")
        fn_body = src[fn_start:fn_start + 4500]
        assert 'include: str = "full"' in fn_body, "get_messages 缺 include 参数"
        slim_pos = fn_body.index("include == \"slim\"") if "include == \"slim\"" in fn_body else fn_body.index("include == 'slim'")
        resolve_pos = fn_body.index("resolve_dict_fields")
        assert slim_pos < resolve_pos, "slim 分支必须先于 resolve 分流"
        assert "slim_message_dict" in fn_body, "slim 分支必须走 slim_message_dict"

    def test_slim_cuts_array_strings_but_exempts_body_segments(self):
        """A4.9 二波 I1：数组内长串照剪（桩不得 100% 携带肥字段）；
        content_segments（正文段数组）整键豁免（正文红线不连带被剪）。"""
        fat = json.dumps({
            "lines": ["x" * 3000, "y" * 3000],
            "content_segments": ["正文段一。" * 400],  # 2000 字正文段，必须原样
        }, ensure_ascii=False)
        out = slim_message_dict({"tool_results": fat})
        stub = out["tool_results"]
        assert is_externalized_stub(stub)
        assert "x" * 3000 not in stub and "y" * 3000 not in stub, "数组长串必须剪"
        assert "正文段一。" * 400 in stub, "content_segments 正文豁免不得连带剪"

    def test_slim_cut_flag_not_content_sniff(self):
        """A4.9 二波：出桩判定用剪裁标记而非内容嗅探——内容里引用 __truncated__
        字样的 thin 字段不得被误判截断。"""
        tricky = json.dumps({"display_sequence": [
            {"type": "tool", "name": "t", "content": "提到 __truncated__ 字样的短内容"}]})
        out = slim_message_dict({"tool_results": tricky})
        assert out["tool_results"] == tricky, "thin 字段（无剪裁）必须原样，禁内容嗅探"


class TestSlimEngineDebt:
    """欠账清零（2026-10-06 三波 A1-A4）：tool_calls 形态对齐 I5 · __size_bytes__
    累计 · 嵌套剪裁项级标记 · thin 免建桩热路。"""

    def test_slim_stubs_tool_calls_items_shape(self):
        # A4.9 B-I7 逆转 D-13：tool_calls 出桩防 MB 级 arguments 满载；
        # 数组载荷以 {meta, items} 交付（消费端解包，写侧 DB 数组恒定 I5 不破）
        big = json.dumps([{"id": "c1", "function": {"name": "f", "arguments": "a" * 3000}}])
        out = slim_message_dict({"tool_calls": big})
        stub = json.loads(out["tool_calls"])
        assert stub.get("_externalized") is True, "tool_calls 必须出桩（防满载）"
        assert isinstance(stub.get("items"), list), "数组载荷 items 解包形"
        assert "a" * 3000 not in out["tool_calls"], "长 arguments 必须被剪"

    def test_size_bytes_accumulates_across_cuts(self):
        item = json.dumps({"display_sequence": [
            {"type": "tool", "name": "t", "title": "T" * 2000, "content": "c" * 3000}]})
        out = slim_message_dict({"tool_results": item})
        stub = json.loads(out["tool_results"])
        seq = stub["display_sequence"][0]
        # N=Σ全量文本（A4.9 C2 双侧同构口径）：type/name/title/content 全计
        expect = 2000 + 3000  # 标签键不计（口径定稿）
        assert seq.get("__size_bytes__") == expect, \
            "N=项文本总量（跨取回恒稳，多切不失真）"

    def test_nested_cut_marks_containing_item(self):
        # 项内嵌套数组长串被剪 → 项级 __truncated__/累计（旧实现无逐项标记）
        item = json.dumps({"display_sequence": [
            {"type": "tool", "name": "t", "title": "短标题",
             "lines": ["z" * 2500, "w" * 2500]}]})
        out = slim_message_dict({"tool_results": item})
        stub = json.loads(out["tool_results"])
        seq = stub["display_sequence"][0]
        assert seq.get("__truncated__") is True, "嵌套剪裁必须落项级标记"
        expect = len("短标题") + 2500 + 2500
        assert seq.get("__size_bytes__") == expect, "嵌套切计入项总量 N"

    def test_thin_field_skips_stub_build(self):
        # 热路：thin 字段不得触发任何建桩（sha/parse/dumps 开销）——钉真实调用点
        import app.services.message_payload_service as svc
        orig_ex, orig_plain = svc.build_field_stub_ex, svc.build_field_stub

        def _bomb(*a, **k):
            raise AssertionError("thin 字段不得建桩（热路开销）")
        svc.build_field_stub_ex = _bomb
        svc.build_field_stub = _bomb
        try:
            out = slim_message_dict({"reasoning_content": "短思考", "content": "正文"})
            assert out["reasoning_content"] == "短思考"
        finally:
            svc.build_field_stub_ex = orig_ex
            svc.build_field_stub = orig_plain


    def test_size_marker_not_spoofable(self):
        """A4.9 M5：内容自带 __size_bytes__ 键不得抬/压徽标 N（整值覆盖）。"""
        tricky = json.dumps({"display_sequence": [
            {"type": "tool", "name": "t", "content": "c" * 3000,
             "__size_bytes__": 1}]})
        out = slim_message_dict({"tool_results": tricky})
        seq = json.loads(out["tool_results"])["display_sequence"][0]
        expect = 3000
        assert seq.get("__size_bytes__") == expect, "伪造键必须被真实总量覆盖"
