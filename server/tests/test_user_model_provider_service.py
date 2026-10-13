"""TDD — 用户模型供应商服务（parse/serialize/build/cache）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_user_model_provider_service.py -v
"""
import asyncio
from types import SimpleNamespace

import pytest

import app.services.user_model_provider_service as svc


class TestParseOverridesPayload:
    def test_valid_payload_normalized(self):
        out = svc.parse_overrides_payload({
            "llm": {
                "enabled": True, "base_url": "https://u.example/v1", "api_key": "sk-x",
                "model_name": "u-model", "params": {"temperature": 0.7, "top_p": None},
            }
        })
        assert out["llm"]["enabled"] is True
        assert out["llm"]["base_url"] == "https://u.example/v1"
        assert out["llm"]["api_key"] == "sk-x"
        assert out["llm"]["model_name"] == "u-model"
        assert out["llm"]["params"] == {"temperature": 0.7, "top_p": None}

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"image": {"base_url": "https://x/v1"}})

    def test_bad_url_scheme_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"base_url": "ftp://x/v1"}})

    def test_empty_url_allowed(self):
        out = svc.parse_overrides_payload({"llm": {"base_url": ""}})
        assert out["llm"]["base_url"] == ""

    def test_unknown_param_key_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"params": {"secret_sauce": 1}}})

    def test_param_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"params": {"temperature": 3.0}}})
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"params": {"top_p": 1.5}}})
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"params": {"top_k": -1}}})

    def test_bool_param_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"params": {"temperature": True}}})

    def test_api_key_none_keeps_empty_clears(self):
        out = svc.parse_overrides_payload({
            "llm": {"api_key": None},
            "vlm": {"api_key": ""},
        })
        assert out["llm"]["api_key"] is None      # 保持原值
        assert out["vlm"]["api_key"] == ""        # 显式清除

    def test_kind_null_marks_deletion(self):
        out = svc.parse_overrides_payload({"tts": None})
        assert out["tts"] is None

    def test_url_too_long_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm": {"base_url": "https://x/" + "a" * 600}})


_FAKE_KEY = "unit" + "-" + "test" + "-" + "ab12"


class TestProviderDimension:
    """逐供应商 LLM 覆写（2026-09-15）：键格式 llm:<alias>。"""

    def test_parse_llm_provider_key(self):
        out = svc.parse_overrides_payload(
            {"llm:qwen3.8": {"enabled": True, "model_name": "u-q"}},
            known_llm_aliases={"qwen3.8", "doubao", "main"},
        )
        assert out["llm:qwen3.8"]["model_name"] == "u-q"

    def test_parse_unknown_provider_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload(
                {"llm:nope": {"model_name": "x"}},
                known_llm_aliases={"qwen3.8", "doubao"},
            )

    def test_parse_provider_key_requires_llm_kind(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"vlm:qwen3.8": {"model_name": "x"}})

    def test_parse_provider_without_alias_rejected(self):
        with pytest.raises(ValueError):
            svc.parse_overrides_payload({"llm:": {"model_name": "x"}})

    def test_parse_provider_null_deletes(self):
        out = svc.parse_overrides_payload({"llm:qwen3.8": None})
        assert out["llm:qwen3.8"] is None

    def test_build_overrides_provider_rows(self):
        rows = [
            self._row(kind="llm", provider="qwen3.8"),
            self._row(kind="llm", provider=""),
            self._row(kind="vlm", provider=""),
        ]
        out = svc.build_overrides_from_rows(rows)
        assert set(out) == {"llm:qwen3.8", "llm", "vlm"}

    def test_serialize_provider_rows(self):
        out = svc.serialize_status([self._row(kind="llm", provider="qwen3.8")])
        assert out["overrides"]["llm:qwen3.8"]["has_api_key"] is True
        assert out["overrides"]["llm:qwen3.8"]["api_key_tail"] == "ab12"
        assert out["overrides"]["llm"] is None
        assert out["overrides"]["vlm"] is None

    def _row(self, **kw):
        base = dict(kind="llm", provider="", enabled=True, base_url="https://u.example/v1",
                    api_key=_FAKE_KEY, model_name="u-model",
                    params_json='{"temperature": 0.7}')
        base.update(kw)
        return SimpleNamespace(**base)


class TestBuildAndSerialize:
    def _row(self, **kw):
        base = dict(kind="llm", enabled=True, base_url="https://u.example/v1",
                    api_key=_FAKE_KEY, model_name="u-model",
                    params_json='{"temperature": 0.7}')
        base.update(kw)
        return SimpleNamespace(**base)

    def test_build_overrides_from_rows(self):
        out = svc.build_overrides_from_rows([self._row()])
        assert out["llm"]["base_url"] == "https://u.example/v1"
        assert out["llm"]["api_key"] == _FAKE_KEY
        assert out["llm"]["params"] == {"temperature": 0.7}

    def test_build_ignores_unknown_kind_rows(self):
        out = svc.build_overrides_from_rows([self._row(kind="image")])
        assert out == {}

    def test_build_tolerates_bad_params_json(self):
        out = svc.build_overrides_from_rows([self._row(params_json="{bad")])
        assert out["llm"]["params"] == {}

    def test_serialize_masks_api_key(self):
        out = svc.serialize_status([self._row()])
        llm = out["overrides"]["llm"]
        assert llm["has_api_key"] is True
        assert llm["api_key_tail"] == "ab12"
        assert "sk-secret-ab12" not in str(out)
        assert llm["params"] == {
            "temperature": 0.7, "top_p": None, "top_k": None,
            "max_tokens": None, "presence_penalty": None, "frequency_penalty": None,
        }

    def test_serialize_missing_kind_is_none(self):
        out = svc.serialize_status([])
        assert out["overrides"]["llm"] is None
        assert out["kinds"] == svc.SUPPORTED_KINDS

    def test_serialize_short_key_tail_masked(self):
        out = svc.serialize_status([self._row(api_key="abc")])
        # A4.9 Minor 修复：≤4 字符 Key 不整串回显
        assert out["overrides"]["llm"]["has_api_key"] is True
        assert out["overrides"]["llm"]["api_key_tail"] == "****"


class TestLoadCache:
    def test_load_uses_cache_and_invalidate(self):
        calls = {"n": 0}

        async def _fake_fetch(db, user_id):
            calls["n"] += 1
            return [SimpleNamespace(kind="llm", enabled=True, base_url="https://u/v1",
                                    api_key="k", model_name="m", params_json=None)]

        orig = svc._fetch_rows
        svc._fetch_rows = _fake_fetch
        try:
            async def _run():
                db = SimpleNamespace()
                a = await svc.load_user_overrides(db, "u1")
                b = await svc.load_user_overrides(db, "u1")
                assert a["llm"]["base_url"] == "https://u/v1"
                assert b["llm"]["base_url"] == "https://u/v1"
                assert calls["n"] == 1
                svc.invalidate_user_overrides("u1")
                await svc.load_user_overrides(db, "u1")
                assert calls["n"] == 2
            asyncio.run(_run())
        finally:
            svc._fetch_rows = orig
            svc.invalidate_user_overrides("u1")

    def test_cache_capacity_bound(self):
        """A4.9 Minor：进程缓存容量有上限（≤256）。"""
        async def _fake_fetch(db, user_id):
            return []

        orig = svc._fetch_rows
        svc._fetch_rows = _fake_fetch
        try:
            async def _run():
                db = SimpleNamespace()
                for i in range(300):
                    await svc.load_user_overrides(db, f"cap-user-{i}")
                assert len(svc._cache) <= 256
            asyncio.run(_run())
        finally:
            svc._fetch_rows = orig
            svc._cache.clear()
