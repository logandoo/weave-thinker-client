"""TDD tests for conversation CSV export hardening (wave 2026-09-18, acceptance #5).

Run from backend/:  ../.venv/bin/python -m pytest tests/test_conversation_csv_export.py -v
"""
import csv
import io
import re

from app.api.conversation import (
    _CSV_CELL_MAX_CHARS,
    _CSV_CELL_MAX_NEWLINES,
    _CSV_PART_HEADER,
    _build_conversation_csv,
    _sanitize_csv_text,
)


def _read_back(text: str) -> tuple[list[dict], list[str]]:
    """Parse CSV text the way an importer would; merge part=k/n continuations."""
    body = text.lstrip("\ufeff")
    records = list(csv.reader(io.StringIO(body)))
    assert records, "csv must not be empty"
    header = [c.strip().lower() for c in records[0]]
    qi, ai = header.index("query"), header.index("answer")
    pi = header.index(_CSV_PART_HEADER) if _CSV_PART_HEADER in header else None

    rows: list[dict] = []
    current = None
    for rec in records[1:]:
        q = rec[qi] if qi < len(rec) else ""
        a = rec[ai] if ai < len(rec) else ""
        part = (rec[pi] if pi is not None and pi < len(rec) else "").strip()
        m = re.match(r"^(\d+)\s*/\s*(\d+)$", part)
        if current and m and int(m.group(1)) > 1:
            current["query"] += q
            current["answer"] += a
        else:
            if current:
                rows.append(current)
            current = {"query": q, "answer": a}
    if current:
        rows.append(current)
    return rows, header


class TestCellSanitize:
    def test_newlines_normalized(self):
        assert _sanitize_csv_text("a\r\nb\rc\nd") == "a\nb\nc\nd"

    def test_control_chars_removed_but_tab_kept(self):
        assert _sanitize_csv_text("a\x00b\x0bc\x1fd\x7f\te") == "abcd\te"

    def test_plain_text_untouched(self):
        assert _sanitize_csv_text("hello, 「世界」\n第二行") == "hello, 「世界」\n第二行"


class TestConversationCsvRoundTrip:
    def test_simple_rows(self):
        rows = [{"query": "q1", "answer": "a1"}, {"query": "q2", "answer": "a2"}]
        parsed, header = _read_back(_build_conversation_csv(rows))
        assert header == ["query", "answer", _CSV_PART_HEADER]
        assert parsed == rows

    def test_commas_quotes_newlines_roundtrip(self):
        rows = [
            {"query": '他说:"你好", 然后\n换行', "answer": 'col,1,col2\n"quoted"\n| a | b |\n|---|---|'},
            {"query": "", "answer": "只有回答"},
        ]
        parsed, _ = _read_back(_build_conversation_csv(rows))
        assert parsed == rows

    def test_long_cell_is_split_and_rejoined_exactly(self):
        long_answer = ("X" * 99 + "\n") * 400  # 40_000 chars, 400 newlines
        rows = [{"query": "长回答", "answer": long_answer}]
        text = _build_conversation_csv(rows)
        parsed, _ = _read_back(text)
        assert parsed == rows, "part merge must be lossless"

        body = text.lstrip("\ufeff")
        for rec in list(csv.reader(io.StringIO(body)))[1:]:
            for cell in rec:
                assert len(cell) <= _CSV_CELL_MAX_CHARS
                assert cell.count("\n") <= _CSV_CELL_MAX_NEWLINES

    def test_long_query_is_split_too(self):
        long_query = "问题" * 20000  # 40_000 chars
        rows = [{"query": long_query, "answer": "短回答"}]
        parsed, _ = _read_back(_build_conversation_csv(rows))
        assert parsed == rows

    def test_cells_at_limit_not_split(self):
        rows = [{"query": "q", "answer": "Y" * _CSV_CELL_MAX_CHARS}]
        body = _build_conversation_csv(rows).lstrip("\ufeff")
        records = list(csv.reader(io.StringIO(body)))
        assert len(records) == 2, "cell at the char limit must stay in one row"
        assert records[1][_CSV_PART_HEADER_IDX] == ""

    def test_bom_prefixed_for_excel(self):
        text = _build_conversation_csv([{"query": "q", "answer": "a"}])
        assert text.startswith("\ufeff")

    def test_control_chars_sanitized_before_write(self):
        rows = [{"query": "a\x00b", "answer": "c\x1fd"}]
        text = _build_conversation_csv(rows)
        assert "\x00" not in text and "\x1f" not in text
        parsed, _ = _read_back(text)
        assert parsed == [{"query": "ab", "answer": "cd"}]


_CSV_PART_HEADER_IDX = 2


class _StubMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class TestFormulaEscaping:
    def test_formula_like_content_escaped_then_restored(self):
        from app.api.conversation import _parse_export_csv

        rows = [
            {"query": "=SUM(A1)", "answer": "@echo off\n- 列表项\n+加号\n普通文本"},
            {"query": "'=already", "answer": "'普通"},
        ]
        parsed, _ = _parse_export_csv(_build_conversation_csv(rows))
        assert parsed == rows

    def test_raw_csv_contains_apostrophe_guard(self):
        text = _build_conversation_csv([{"query": "=1+1", "answer": "a"}])
        body = text.lstrip("\ufeff")
        assert "'=1+1" in body


class TestUtf16Chunking:
    def test_astral_chars_counted_as_two_units(self):
        from app.api.conversation import _parse_export_csv, _utf16_len

        cell = "😀" * 16000  # 16000 code points = 32000 UTF-16 units
        rows = [{"query": "q", "answer": cell}]
        parsed, _ = _parse_export_csv(_build_conversation_csv(rows))
        assert parsed == rows
        records = list(csv.reader(io.StringIO(_build_conversation_csv(rows).lstrip("\ufeff"))))
        for rec in records[1:]:
            assert _utf16_len(rec[1]) <= _CSV_CELL_MAX_CHARS


class TestOrphanAssistantExport:
    def test_orphan_assistant_messages_preserved(self):
        from app.api.conversation import _messages_to_rows

        msgs = [
            _StubMessage("assistant", "我先说话"),
            _StubMessage("user", "u1"),
            _StubMessage("assistant", "a1"),
            _StubMessage("assistant", "孤儿回答"),
        ]
        assert _messages_to_rows(msgs) == [
            {"query": "", "answer": "我先说话"},
            {"query": "u1", "answer": "a1"},
            {"query": "", "answer": "孤儿回答"},
        ]


class TestSplitBoundaryFormula:
    def test_continuation_chunk_apostrophe_not_stripped(self):
        from app.api.conversation import _parse_export_csv

        cell = "a" * 30000 + "'=evil-rest"  # chunk 2 begins with apostrophe + =
        rows = [{"query": "q", "answer": cell}]
        parsed, errors = _parse_export_csv(_build_conversation_csv(rows))
        assert parsed == rows, "continuation chunk must not lose its leading apostrophe"
        assert errors == []

    def test_escaped_formula_with_split_tail_roundtrips(self):
        from app.api.conversation import _parse_export_csv

        cell = "=" + "a" * 29998 + "'=tail"
        rows = [{"query": "q", "answer": cell}]
        parsed, _ = _parse_export_csv(_build_conversation_csv(rows))
        assert parsed == rows


class TestZipMemberLimit:
    def test_streaming_member_read_respects_budget(self):
        import zipfile

        from app.api.conversation import _read_zip_member_limited

        payload = b"A" * (5 * 1024 * 1024)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("big.csv", payload)
        buf.seek(0)
        with zipfile.ZipFile(buf) as zf:
            info = zf.infolist()[0]
            assert _read_zip_member_limited(zf, info, 1 * 1024 * 1024) is None
            got = _read_zip_member_limited(zf, info, 6 * 1024 * 1024)
            assert got == payload


class TestParserRowCap:
    def test_row_cap_enforced_during_parse(self):
        from app.api.conversation import _IMPORT_MAX_ROWS_PER_CSV, _parse_export_csv

        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["query", "answer"])
        for i in range(_IMPORT_MAX_ROWS_PER_CSV + 50):
            w.writerow([f"q{i}", f"a{i}"])
        parsed, errors = _parse_export_csv(buf.getvalue())
        assert len(parsed) == _IMPORT_MAX_ROWS_PER_CSV
        assert any("行数超过" in e for e in errors)
