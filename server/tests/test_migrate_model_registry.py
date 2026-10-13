"""TDD tests for migrate_model_registry script（2026-08-31 两文件合并版）.

合并模式：model_registry.toml → config_model.toml（endpoints/routing 段并入，
deathmatch.vlm→vlm 规范改名，registry 文件删除）；再生成模式：无 registry 时从
配置 synthesize 出段写入。
Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_migrate_model_registry.py -v
"""
import toml


CONF = {
    "api": {"base_url": "https://api.deepseek.com/v1", "api_key": "sk-main", "model_name": "deepseek-chat"},
    "defaults": {"temperature": 0.7, "top_p": 1.0, "max_tokens": 4096},
}


class TestGenerateSections:
    def test_generates_all_core_endpoints(self):
        from scripts.migrate_model_registry import generate_sections_toml
        doc = toml.loads(generate_sections_toml(CONF))
        eps = doc["endpoints"]
        for alias in ("main", "deepseek", "embedding", "rerank", "asr", "tts", "vlm"):
            assert alias in eps, f"missing endpoint {alias}"
        assert eps["main"]["model_name"] == "deepseek-chat"
        assert eps["main"]["api_key"] == "sk-main"

    def test_idempotent(self):
        from scripts.migrate_model_registry import generate_sections_toml
        assert generate_sections_toml(CONF) == generate_sections_toml(CONF)


class TestBuildConsolidation:
    def _write(self, p, text):
        p.write_text(text, encoding="utf-8")
        return p

    def test_merge_mode_renames_vlm_and_rewrites_stale_routing(self, tmp_path):
        from scripts.migrate_model_registry import build_consolidation
        model = self._write(tmp_path / "config_model.toml", '[api]\nmodel_name = "x"\n')
        registry = self._write(tmp_path / "model_registry.toml", """
[endpoints.main]
kind = "llm"
base_url = "https://api.deepseek.com/v1"
api_key = "sk-main"
model_name = "deepseek-chat"

[endpoints."deathmatch.vlm"]
kind = "llm"
base_url = "http://vlm/v1"
api_key = "sk-v"
model_name = "mm-1"

[routing."vlm"]
alias = "deathmatch.vlm"

[routing."deathmatch.judge"]
alias = "deepseek"
""")
        block, summary = build_consolidation(model, registry)
        assert summary["mode"] == "merge"
        doc = toml.loads(block)
        assert "vlm" in doc["endpoints"], "deathmatch.vlm must be renamed to canonical vlm"
        assert "deathmatch.vlm" not in doc["endpoints"]
        assert doc["endpoints"]["vlm"]["base_url"] == "http://vlm/v1"
        assert doc["routing"]["vlm"]["alias"] == "vlm", (
            "routing to the renamed alias must be REWRITTEN to the canonical alias"
        )
        assert doc["routing"]["deathmatch.judge"]["alias"] == "deepseek"

    def test_refuses_second_run(self, tmp_path):
        from scripts.migrate_model_registry import build_consolidation
        import pytest
        model = self._write(tmp_path / "config_model.toml", '[endpoints.main]\nmodel_name = "x"\n')
        registry = tmp_path / "model_registry.toml"  # absent
        with pytest.raises(ValueError, match="只能执行一次"):
            build_consolidation(model, registry)

    def test_regenerate_mode_without_registry(self, tmp_path, monkeypatch):
        from scripts import migrate_model_registry as mig
        model = self._write(tmp_path / "config_model.toml", '[api]\nmodel_name = "x"\n')
        registry = tmp_path / "model_registry.toml"  # absent
        monkeypatch.setattr(mig, "get_config", lambda: type("C", (), {"_config": CONF})())
        block, summary = mig.build_consolidation(model, registry)
        assert summary["mode"] == "regenerate"
        doc = toml.loads(block)
        assert doc["endpoints"]["main"]["model_name"] == "deepseek-chat"
