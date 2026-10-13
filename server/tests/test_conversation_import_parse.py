"""TDD tests for conversation CSV import parsing (wave 2026-09-18, acceptance #6/#7).

Run from backend/:  ../.venv/bin/python -m pytest tests/test_conversation_import_parse.py -v
"""
import csv
import io

from app.api.conversation import (
    _build_conversation_csv,
    _conversation_title_from_filename,
    _parse_export_csv,
)

UUID = "6f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def _csv_text(rows, header=("query", "answer")):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


class TestTitleFromFilename:
    def test_strips_extension_and_uuid_suffix(self):
        assert _conversation_title_from_filename(f"周报记录_{UUID}.csv") == "周报记录"

    def test_plain_name_kept(self):
        assert _conversation_title_from_filename("my-chat.csv") == "my-chat"

    def test_empty_falls_back(self):
        assert _conversation_title_from_filename(".csv") == "导入的会话"

    def test_path_prefix_ignored(self):
        assert _conversation_title_from_filename(f"sub/dir/标题_{UUID}.csv") == "标题"


class TestParseExportCsv:
    def test_export_roundtrip_lossless(self):
        rows = [
            {"query": "q1", "answer": 'a,1 "引号"\n第二行'},
            {"query": "长", "answer": ("Y" * 99 + "\n") * 400},
        ]
        parsed, errors = _parse_export_csv(_build_conversation_csv(rows))
        assert errors == []
        assert parsed == rows

    def test_legacy_two_column_header(self):
        text = _csv_text([["q1", "a1"], ["q2", "a2"]])
        parsed, errors = _parse_export_csv(text)
        assert errors == []
        assert parsed == [{"query": "q1", "answer": "a1"}, {"query": "q2", "answer": "a2"}]

    def test_headerless_treated_as_data(self):
        text = _csv_text([["q1", "a1"]], header=("question", "response"))
        parsed, errors = _parse_export_csv(text)
        assert parsed == [{"query": "question", "answer": "response"}, {"query": "q1", "answer": "a1"}]

    def test_bom_is_stripped(self):
        text = "\ufeff" + _csv_text([["q1", "a1"]])
        parsed, _ = _parse_export_csv(text)
        assert parsed == [{"query": "q1", "answer": "a1"}]

    def test_quoted_newlines_preserved(self):
        text = _csv_text([["多行问题\n第二行", "多行回答\n第三行"]])
        parsed, _ = _parse_export_csv(text)
        assert parsed == [{"query": "多行问题\n第二行", "answer": "多行回答\n第三行"}]

    def test_malformed_part_starts_new_row_without_crash(self):
        text = _csv_text(
            [["q1", "a1", "1/2"], ["q1b", "a1b", "garbage"]],
            header=("query", "answer", "part"),
        )
        parsed, errors = _parse_export_csv(text)
        assert parsed == [{"query": "q1", "answer": "a1"}, {"query": "q1b", "answer": "a1b"}]
        assert len(errors) >= 1

    def test_empty_file_reports_error(self):
        parsed, errors = _parse_export_csv("")
        assert parsed == []
        assert errors

    def test_empty_rows_skipped(self):
        text = _csv_text([["", ""], ["q", ""], ["", "a"]])
        parsed, _ = _parse_export_csv(text)
        assert parsed == [{"query": "q", "answer": ""}, {"query": "", "answer": "a"}]


class TestMalformedPart:
    def test_sequence_gap_reported_and_row_split(self):
        text = _csv_text(
            [["q", "a", "1/3"], ["x", "y", "3/3"], ["z", "w", "2/2"]],
            header=("query", "answer", "part"),
        )
        parsed, errors = _parse_export_csv(text)
        assert parsed == [
            {"query": "q", "answer": "a"},
            {"query": "x", "answer": "y"},
            {"query": "z", "answer": "w"},
        ]
        assert any("不连续" in e for e in errors)

    def test_zero_index_reported(self):
        text = _csv_text([["q", "a", "0/2"]], header=("query", "answer", "part"))
        parsed, errors = _parse_export_csv(text)
        assert parsed == [{"query": "q", "answer": "a"}]
        assert errors

    def test_huge_part_digits_do_not_crash(self):
        text = _csv_text(
            [["q", "a", "9" * 5000 + "/2"]], header=("query", "answer", "part")
        )
        parsed, errors = _parse_export_csv(text)
        assert parsed == [{"query": "q", "answer": "a"}]
        assert errors

    def test_oversized_field_reported_not_raised(self):
        text = '"query","answer"\r\n"q","' + "x" * 200000 + '"'
        parsed, errors = _parse_export_csv(text)
        assert parsed == []
        assert errors


class TestTitleHardening:
    def test_title_capped(self):
        long_name = "标题" * 200 + f"_{UUID}.csv"
        assert len(_conversation_title_from_filename(long_name)) <= 100

    def test_bidi_and_zero_width_stripped(self):
        name = "a\u202eb\u200b" + ".csv"
        title = _conversation_title_from_filename(name)
        assert "\u202e" not in title and "\u200b" not in title
