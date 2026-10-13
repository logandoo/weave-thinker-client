"""TDD tests for leading-orphan-colon strip (conv e7d51dcb '：' fix).

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_leading_colon_strip.py -v
"""
import pytest

from app.api.chat import (
    _compose_terminal_content,
    _strip_leading_orphan_colon,
)


class TestStripLeadingOrphanColon:
    def test_fullwidth_colon_stripped(self):
        assert _strip_leading_orphan_colon("：没有") == "没有"

    def test_ascii_colon_stripped(self):
        assert _strip_leading_orphan_colon(":没有") == "没有"

    def test_bold_after_colon(self):
        # the exact production shape (conv e7d51dcb): '：' + '**没有**'
        assert _strip_leading_orphan_colon("：**没有**。AMD") == "**没有**。AMD"

    def test_colon_only_string_preserved(self):
        # never empty out a message: a bare colon is kept as-is
        assert _strip_leading_orphan_colon("：") == "："
        assert _strip_leading_orphan_colon(":") == ":"

    def test_colon_followed_only_by_whitespace_preserved(self):
        assert _strip_leading_orphan_colon("：   ") == "：   "

    def test_no_leading_colon_byte_identical(self):
        # the no-op path must return the SAME object (pins byte-identity)
        text = "没有。AMD Ryzen AI Max+ 395 集成的 Radeon 8060S 属于 RDNA 3.5"
        assert _strip_leading_orphan_colon(text) is text

    def test_ascii_emoticons_preserved(self):
        # A4.9 M4: ASCII ':' heads with legitimate uses survive
        assert _strip_leading_orphan_colon(":) 哈哈") == ":) 哈哈"
        assert _strip_leading_orphan_colon(":-) 笑") == ":-) 笑"
        assert _strip_leading_orphan_colon("::foo_bar") == "::foo_bar"
        assert _strip_leading_orphan_colon(":-x") == ":-x"

    def test_ascii_colon_stripped_for_content(self):
        assert _strip_leading_orphan_colon(": 注意一下") == " 注意一下"

    def test_empty_and_none_safe(self):
        assert _strip_leading_orphan_colon("") == ""

    def test_only_one_colon_stripped(self):
        # exactly ONE orphan colon — a second colon is content (e.g. a list)
        assert _strip_leading_orphan_colon("：:x") == ":x"

    def test_leading_whitespace_kept_colon_stripped(self):
        assert _strip_leading_orphan_colon("  ：没有") == "  没有"

    def test_url_style_colon_not_at_head_untouched(self):
        assert _strip_leading_orphan_colon("见 https://x.y/：补充") == "见 https://x.y/：补充"


class TestComposeTerminalContent:
    def test_leading_colon_removed_content_preserved(self):
        content, substituted = _compose_terminal_content("：没有。后续完整回答。", None, True)
        assert substituted is False
        assert content == "没有。后续完整回答。"

    def test_ascii_leading_colon_removed(self):
        content, substituted = _compose_terminal_content(":nope", None, True)
        assert content == "nope" and substituted is False

    def test_clean_content_byte_identical(self):
        content, substituted = _compose_terminal_content("没有。", None, False)
        assert content == "没有。" and substituted is False

    def test_empty_with_error_still_substitutes(self):
        content, substituted = _compose_terminal_content("", "boom", True)
        assert substituted is True and "回答生成失败" in content

    def test_empty_no_error_no_tools_stays_empty(self):
        content, substituted = _compose_terminal_content("   ", None, False)
        assert content == "" and substituted is False

    def test_bare_colon_content_not_emptied(self):
        # a degenerate colon-only answer must NOT become the failure-text branch
        content, substituted = _compose_terminal_content("：", None, True)
        assert substituted is False
        assert content.startswith("：")
