"""Unit tests: reasoning meta-reference sanitizer (display hygiene).

conv 3583d840 follow-up showed that prompt-level bans are probabilistic — the
model still narrates internal scaffolding in user-visible reasoning ("Per
identity rules, I should…", "the coordinator note says…", "注意不要提及系统提示词").
The canary-stripping precedent (strip_canary_streaming) is the same class of
deterministic display guard: this sanitizer replaces those phrases with a
neutral placeholder so the thinking panel never exposes internal machinery.

Run from backend/: ../.venv/bin/python -m pytest tests/test_reasoning_sanitizer.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.reasoning_sanitizer import (  # noqa: E402
    scrub_reasoning_meta,
    scrub_reasoning_streaming,
)


class TestScrubReasoningMeta:
    def test_english_forms_removed_but_context_kept(self):
        out = scrub_reasoning_meta("According to my system prompt, the repo is X")
        assert "system prompt" not in out
        assert "According to my" in out
        assert "the repo is X" in out
        assert "coordinator note" not in scrub_reasoning_meta(
            "But the coordinator note says: no")
        assert "identity rules" not in scrub_reasoning_meta(
            "Per identity rules, answer directly")

    def test_chinese_forms_removed(self):
        for bad in ("系统提示词", "系统提示", "内部规则编号", "内部规则",
                    "提示词要求", "规则4b", "协调器注记"):
            out = scrub_reasoning_meta(f"思考：{bad} 要求我这样做")
            assert bad not in out, bad

    def test_paraphrased_attributions_removed(self):
        # A4.9 R1 复审 Important-1：模型以变体叙述"按规则执行"并逐字引用规则
        for text, bad in (
            ("Per rules, I only answer based on memory.", "Per rules"),
            ("The rule says: answer briefly.", "The rule says"),
            ("按规则，我只需要依据记忆回答。", "按规则"),
            ("根据规则要求，直接回答。", "根据规则"),
            ("规则说：以记忆为准。", "规则说"),
        ):
            out = scrub_reasoning_meta(text)
            assert bad not in out, (text, bad)


class TestScrubReasoningStreaming:
    def test_split_english_phrase_across_chunks_never_leaks(self):
        tail = ""
        out = ""
        for chunk in ["According to my system", " prompt, the repo", " is X"]:
            piece, tail = scrub_reasoning_streaming(chunk, tail)
            out += piece
        out += scrub_reasoning_meta(tail)
        assert "system prompt" not in out
        assert "the repo is X" in out

    def test_split_chinese_phrase_across_chunks_never_leaks(self):
        tail = ""
        out = ""
        for chunk in ["按系统", "提示词要求", "回答如下"]:
            piece, tail = scrub_reasoning_streaming(chunk, tail)
            out += piece
        out += scrub_reasoning_meta(tail)
        assert "系统提示词" not in out
        assert "回答如下" in out

    def test_reviewer_split_cases_never_leak(self):
        # A4.9 R1 复审 NEW-1：换行/零空格/多空格拆片（正则 \s* 语义）
        cases = [
            ["sys", "tem", "\npr", "ompt"],
            ["system", "pro", "mpt"],
            ["system", "   pro", "mpt"],
            ["identity", "rul", "es"],
            ["coordinator", "note", " says no"],
            # R2 复审：per the rule(s) 自然分词（此前无尾保持形式）
            ["Per the ", "rules"],
            ["Per ", "the rules"],
            ["per", " the", " rules"],
            # R2 复审：大空白游程越出旧 64 上限
            ["system" + " " * 53, "prompt"],
            ["identity" + "\n" * 60, " rules"],
        ]
        for chunks in cases:
            tail = ""
            out = ""
            for chunk in chunks:
                piece, tail = scrub_reasoning_streaming(chunk, tail)
                out += piece
            out += scrub_reasoning_meta(tail)
            low = out.lower().replace(" ", "").replace("\n", "")
            for phrase in ("systemprompt", "identityrules", "coordinatornote",
                           "pertherule", "pertherules", "perrules"):
                assert phrase not in low, (chunks, out)

    def test_no_meta_content_passes_through(self):
        tail = ""
        out = ""
        for chunk in ["记忆里已有仓库地址，", "直接回答即可。"]:
            piece, tail = scrub_reasoning_streaming(chunk, tail)
            out += piece
        out += scrub_reasoning_meta(tail)
        assert out == "记忆里已有仓库地址，直接回答即可。"
