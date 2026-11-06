"""视觉评审（visual_critic）2026-11-01 反转配套：端点解析后置 + 走 model_gateway。

A4.9 R1-I2/R2-I4：主模型优先后，解析会带一次行为探针（最坏 60s）——必须
先判定「本轮真的产出了可视交付物」，否则无 .html/.svg 的步骤白探一次。
A4.9 R2-I6：主模型常是思考模型，评审调用必须走 gateway（thinking profile +
max_tokens），不能再用裸 openai 客户端（reasoning-only 回复会静默 None）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_visual_critic_gate.py -v
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _run(coro):
    return asyncio.run(coro)


def test_no_visual_artifact_never_resolves_endpoint(monkeypatch):
    from app.services import visual_critic

    async def _boom(assistant_llm=None):
        raise AssertionError("非可视步骤不得触发端点探测")

    monkeypatch.setattr(visual_critic, "_resolve_vlm", _boom)
    out = _run(visual_critic.critique_visual_artifacts(
        [{"path": "report.md"}, {"path": "data.csv"}],
        {"description": "写报告"}, "goal", workspace_path="/tmp"))
    assert out is None


def test_missing_target_never_resolves_endpoint(monkeypatch):
    from app.services import visual_critic

    async def _boom(assistant_llm=None):
        raise AssertionError("目标文件不存在时不得触发端点探测")

    monkeypatch.setattr(visual_critic, "_resolve_vlm", _boom)
    out = _run(visual_critic.critique_visual_artifacts(
        [{"path": "index.html"}], {}, "goal", workspace_path="/nonexistent-xyz"))
    assert out is None


def test_call_vlm_goes_through_model_gateway(monkeypatch):
    from app.model_gateway.schemas import KIND_LLM, ModelEndpoint
    from app.services import visual_critic

    ep = ModelEndpoint(alias="deepseek", kind=KIND_LLM, base_url="http://m.local/v1",
                       api_key="", model_name="m-1", is_custom=True)
    captured = {}

    class _LLM:
        async def complete_chat_parts(self, messages, **kwargs):
            captured["messages"] = messages
            captured["kwargs"] = kwargs
            return '{"layout_ok": false, "issues": ["内容被裁剪"]}', ""

    def _fake_build(ep_, **k):
        captured["api_key"] = ep_.api_key
        return _LLM()

    monkeypatch.setattr("app.model_gateway.factory.build_llm_service", _fake_build)
    verdict = _run(visual_critic._call_vlm(ep, b"\x89PNG\r\n\x1a\n", {"expected_output": "x"}, "goal"))
    assert verdict == {"layout_ok": False, "issues": ["内容被裁剪"]}
    assert captured["api_key"] == "no-key", "空 key 不得回落系统密钥"
    assert captured["kwargs"].get("max_tokens") == visual_critic._VLM_MAX_TOKENS
    assert captured["messages"][0]["content"][1]["type"] == "image_url"
