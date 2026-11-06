"""C-14: tiktoken stays optional, with a reproducible opt-in path."""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prompts import compiler


def _reset():
    compiler._TIKTOKEN_ENCODER = None
    compiler._TIKTOKEN_TRIED = False


def test_default_does_not_import_tiktoken(monkeypatch):
    _reset()
    monkeypatch.delenv("PROMPT_TOKENIZER", raising=False)
    called = {"n": 0}

    fake = types.ModuleType("tiktoken")

    def _get_encoding(_name):
        called["n"] += 1
        raise AssertionError("must not be imported without opt-in")

    fake.get_encoding = _get_encoding
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    assert compiler.estimate_tokens("中文 hello") > 0
    assert called["n"] == 0


def test_opt_in_uses_tiktoken_when_available(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_TOKENIZER", "tiktoken")

    class _Encoder:
        def encode(self, text):
            return list(range(len(text) // 2 or 1))

    fake = types.ModuleType("tiktoken")
    fake.get_encoding = lambda _name: _Encoder()
    monkeypatch.setitem(sys.modules, "tiktoken", fake)
    assert compiler.estimate_tokens("abcdef") == 3
    _reset()


def test_opt_in_falls_back_when_tiktoken_absent(monkeypatch):
    _reset()
    monkeypatch.setenv("PROMPT_TOKENIZER", "tiktoken")
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    n = compiler.estimate_tokens("中文 hello")
    assert n >= 3
    _reset()
