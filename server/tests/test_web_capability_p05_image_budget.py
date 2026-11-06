"""P0.5（2026-11-01）— 图片信封与字符预算/上下文估算的隔离。

根因：browser_screenshot 返回的 base64 信封在真实尺寸下 >100k chars，被
tool-result 字符预算当作大文本外置（图片丢失）；context compressor 又按
base64 字符数估算 token（≈40x 高估），旧帧无降级。

覆盖：
1. 真实 1280x720 截图信封经 maybe_persist_tool_result / enforce_turn_budget 原样保留
2. expand_tool_images 仍产出 image_url
3. 尺寸感知 token 估算（w*h/750），不再按 base64 字符数
4. 历史 2 帧 + 当前轮 4 帧，较早帧替换为含路径占位
5. 非 allowlist 工具伪造信封不能绕过字符预算；普通大文本仍外置
6. browser_screenshot schema 文案（内联图像 + browser_mouse）
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import tool_image_results as tir  # noqa: E402
from app.services.context_compressor import ContextCompressor, estimate_tokens_rough  # noqa: E402
from app.services.history_window_service import (  # noqa: E402
    estimate_message_tokens,
    select_history_window_meta,
)
from app.services.tool_result_budget import (  # noqa: E402
    BudgetConfig,
    enforce_turn_budget,
    maybe_persist_tool_result,
)


def _write_noise_png(path: Path, size=(1280, 720)) -> Path:
    from PIL import Image

    Image.effect_noise(size, 100).convert("RGB").save(path, format="PNG")
    return path


def _envelope(path: Path, text: str) -> str:
    env = tir.build_image_envelope_from_file(str(path), text)
    assert env is not None
    return env


def _tool_msg(content: str, name: str = "browser_screenshot", call_id: str = "c1") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


def test_real_size_screenshot_envelope_survives_budget(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "浏览器截图已保存：shot.png")
    assert len(env) > 100_000, "fixture must exceed the 100k char budget"

    kept = asyncio.run(maybe_persist_tool_result(env, "browser_screenshot", "call_1", BudgetConfig()))
    assert kept == env
    assert not kept.startswith("<persisted-output>")

    expanded = tir.expand_tool_images([_tool_msg(kept)])
    parts = expanded[0]["content"]
    assert isinstance(parts, list)
    assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in parts)


def test_turn_budget_does_not_externalize_image_envelope(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "浏览器截图已保存：shot.png")

    # Envelope alone exceeds the char budget but must stay inline.
    only = [_tool_msg(env)]
    out = asyncio.run(enforce_turn_budget(only, BudgetConfig(turn_budget_chars=1_000, persist_dir=str(tmp_path / "persist"))))
    assert out[0]["content"] == env

    # A large text result alongside it is still externalized.
    msgs = [
        _tool_msg(env, call_id="c1"),
        _tool_msg("x" * 250_000, name="terminal", call_id="c2"),
    ]
    out = asyncio.run(enforce_turn_budget(msgs, BudgetConfig(persist_dir=str(tmp_path / "persist"))))
    assert out[0]["content"] == env
    assert "<persisted-output>" in out[1]["content"]


def test_image_token_estimate_uses_dimensions_not_base64(tmp_path):
    png720 = _write_noise_png(tmp_path / "shot720.png", (1280, 720))
    png1080 = _write_noise_png(tmp_path / "shot1080.png", (1920, 1080))
    env720 = _envelope(png720, "shot720")
    env1080 = _envelope(png1080, "shot1080")

    est720 = estimate_tokens_rough([_tool_msg(env720)])
    est1080 = estimate_tokens_rough([_tool_msg(env1080)])

    assert 500 <= est720 < 5_000
    assert 1_000 <= est1080 < 8_000
    # base64-char accounting would be ~len/4 (tens of thousands).
    assert est720 < len(env720) // 10
    assert est1080 < len(env1080) // 10


def test_old_image_frames_are_placeholdered(tmp_path, monkeypatch):
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 2))
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [
        _tool_msg(_envelope(png, "shot-0"), call_id="c0"),
        _tool_msg(_envelope(png, "shot-1"), call_id="c1"),
        _tool_msg(_envelope(png, "shot-2"), call_id="c2"),
        {"role": "assistant", "content": "done"},
    ]
    out, pruned = ContextCompressor(model="test", quiet=True)._prune_old_tool_results(msgs)

    assert pruned >= 1
    assert out[0]["content"].startswith("[image omitted from context")
    assert "shot-0" in out[0]["content"]
    assert out[1]["content"].startswith('{"_image_result"')
    assert out[2]["content"].startswith('{"_image_result"')


def test_non_allowlisted_tool_cannot_bypass_char_budget(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "spoof")
    out = asyncio.run(
        maybe_persist_tool_result(
            env, "terminal", "call_spoof", BudgetConfig(persist_dir=str(tmp_path / "persist"))
        )
    )
    assert out.startswith("<persisted-output>")


def test_large_text_still_persisted(tmp_path):
    big = "x" * 150_000
    out = asyncio.run(
        maybe_persist_tool_result(
            big, "terminal", "call_text", BudgetConfig(persist_dir=str(tmp_path / "persist"))
        )
    )
    assert out.startswith("<persisted-output>")


def test_screenshot_schema_mentions_inline_image_and_mouse():
    from app.tools.browser_interact import _SCREENSHOT_SCHEMA

    desc = _SCREENSHOT_SCHEMA["description"].lower()
    assert "image" in desc
    assert "browser_mouse" in desc


def test_expand_caps_old_frames_to_last_two(tmp_path, monkeypatch):
    # P0.5.1：默认变为 turn-aware（2 历史 + 4 当前轮）；此例显式钉旧上限 2/2。
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 2))
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [
        _tool_msg(_envelope(png, "shot-0"), call_id="c0"),
        _tool_msg(_envelope(png, "shot-1"), call_id="c1"),
        _tool_msg(_envelope(png, "shot-2"), call_id="c2"),
    ]
    out = tir.expand_tool_images(msgs)
    assert out[0]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    assert "shot-0" in out[0]["content"]
    image_parts = [
        sum(1 for p in m["content"] if isinstance(p, dict) and p.get("type") == "image_url")
        for m in out[1:]
        if isinstance(m["content"], list)
    ]
    assert image_parts == [1, 1]
    assert len(out) == 3


def test_history_window_costs_tool_results_envelopes(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "shot")
    tool_calls = json.dumps([{
        "id": "call_1",
        "type": "function",
        "function": {"name": "browser_screenshot", "arguments": "{\"full_page\": false}"},
    }])
    tool_results = json.dumps({
        "agent_steps": [{
            "name": "call_1",
            "title": "工具调用 · browser_screenshot",
            "content": env,
            "step_type": "tool",
        }]
    }, ensure_ascii=False)
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": tool_calls,
        "tool_results": tool_results,
    }
    assert estimate_message_tokens(assistant) < 5_000

    msgs = [
        {"role": "user", "content": "take a screenshot"},
        assistant,
        {"role": "user", "content": "what changed?"},
    ]
    selected = select_history_window_meta(msgs, 40, 300_000, mode="2d")
    assert len(selected.messages) == 3
    assert selected.messages[0]["content"] == "take a screenshot"


def test_placeholder_survives_superseded_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 2))
    png = _write_noise_png(tmp_path / "shot.png")
    long_text = "shot-0 " + "x" * 260
    envs = [
        _envelope(png, long_text),
        _envelope(png, "shot-1"),
        _envelope(png, "shot-2"),
    ]
    calls = [
        [{"id": f"c{i}", "type": "function",
          "function": {"name": "browser_screenshot", "arguments": "{\"full_page\": false}"}}]
        for i in range(3)
    ]
    msgs = []
    for i, env in enumerate(envs):
        msgs.append({"role": "assistant", "content": "", "tool_calls": calls[i]})
        msgs.append(_tool_msg(env, call_id=f"c{i}"))
    msgs.append({"role": "assistant", "content": "done"})

    out, pruned = ContextCompressor(model="test", quiet=True)._prune_old_tool_results(msgs)
    first_tool = out[1]["content"]
    assert pruned >= 1
    assert first_tool.startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    assert "shot-0" in first_tool
    assert "saved at" in first_tool
    # The two newest frames stay envelopes; the placeholder is not summarized away.
    assert out[3]["content"].startswith('{"_image_result"')
    assert out[5]["content"].startswith('{"_image_result"')


def test_malformed_image_envelope_not_exempt(tmp_path):
    malformed = json.dumps({
        "_image_result": True,
        "text": "x" * 200_000,
        "images": {"not": "a list"},
    })
    out = asyncio.run(
        maybe_persist_tool_result(
            malformed, "browser_screenshot", "call_bad",
            BudgetConfig(persist_dir=str(tmp_path / "persist")),
        )
    )
    assert out.startswith("<persisted-output>")


def test_dimension_clamp_caps_token_estimate():
    env = tir.build_image_envelope("x", [{
        "mime": "image/png",
        "data_b64": "A" * 1_000,
        "width": 100_000,
        "height": 100_000,
    }])
    est = estimate_tokens_rough([_tool_msg(env)])
    assert est < 30_000


def test_malformed_envelope_does_not_crash_estimator_or_pruner():
    malformed = json.dumps({
        "_image_result": True,
        "text": "x",
        "images": {"not": "a list"},
    })
    msgs = [_tool_msg(malformed)]
    assert estimate_tokens_rough(msgs) > 0
    out, _ = ContextCompressor(model="test", quiet=True)._prune_old_tool_results(msgs)
    assert out and isinstance(out[0]["content"], str)


def test_inline_off_does_not_charge_image_tokens(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "shot")
    monkeypatch.setattr("app.services.context_compressor.inline_images_enabled", lambda: False)
    monkeypatch.setattr("app.services.history_window_service.inline_images_enabled", lambda: False)
    assert estimate_tokens_rough([_tool_msg(env)]) < 500
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": json.dumps([{
            "id": "call_1",
            "type": "function",
            "function": {"name": "browser_screenshot", "arguments": "{}"},
        }]),
        "tool_results": json.dumps({"agent_steps": [{
            "name": "call_1",
            "title": "工具调用 · browser_screenshot",
            "content": env,
        }]}, ensure_ascii=False),
    }
    assert estimate_message_tokens(assistant) < 500


def test_envelope_path_is_basename(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "shot")
    payload = json.loads(env)
    assert payload["images"][0]["path"] == "shot.png"


def test_screenshot_schema_mentions_saved_path():
    from app.tools.browser_interact import _SCREENSHOT_SCHEMA

    assert "saved path" in _SCREENSHOT_SCHEMA["description"].lower()


def test_degraded_pointerize_placeholders_image(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = _envelope(png, "shot-degraded")
    msgs = [
        {"role": "user", "content": "go"},
        _tool_msg(env),
        {"role": "user", "content": "what now?"},
    ]
    out = asyncio.run(ContextCompressor(model="test", quiet=True)._degraded_pointerize(msgs))
    assert out[1]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    assert "saved at" in out[1]["content"]
    assert not out[1]["content"].startswith('{"_image_result"')


def _write_solid_jpeg(path: Path, size=(1920, 1080)) -> Path:
    from PIL import Image

    Image.new("RGB", size, (12, 34, 56)).save(path, format="JPEG", quality=85)
    return path


def test_turn_aware_selection_defaults(tmp_path):
    frames = [_envelope(_write_noise_png(tmp_path / f"f{i}.png", (64, 64)), f"f{i}") for i in range(3)]
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(env, call_id=f"c{i}") for i, env in enumerate(frames)
    ]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
        if m.get("role") == "tool"
    }
    kept = tir.select_kept_image_frames(msgs, parsed, history_limit=2, current_limit=4)
    assert kept == {1, 2, 3}


def test_current_turn_keeps_four(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"shot-{i}"), call_id=f"c{i}") for i in range(5)
    ]
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 4))
    out = tir.expand_tool_images(msgs)
    assert out[1]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    for i in range(2, 6):
        assert isinstance(out[i]["content"], list)
        assert any(p.get("type") == "image_url" for p in out[i]["content"])


def test_history_and_current_split(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [
        {"role": "user", "content": "turn 0"},
        _tool_msg(_envelope(png, "h0"), call_id="h0"),
        _tool_msg(_envelope(png, "h1"), call_id="h1"),
        _tool_msg(_envelope(png, "h2"), call_id="h2"),
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "turn 1"},
        _tool_msg(_envelope(png, "c0"), call_id="c0"),
        _tool_msg(_envelope(png, "c1"), call_id="c1"),
    ]
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 4))
    out = tir.expand_tool_images(msgs)
    assert out[1]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    for i in (2, 3, 6, 7):
        assert isinstance(out[i]["content"], list)


def test_no_user_keeps_last_four(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [_tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)]
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 4))
    out = tir.expand_tool_images(msgs)
    assert out[0]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    for i in range(1, 5):
        assert isinstance(out[i]["content"], list)


def test_zero_frame_limits_keep_none(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [_tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(2)]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
    }
    assert tir.select_kept_image_frames(msgs, parsed, history_limit=0, current_limit=0) == set()


def test_config_vision_defaults():
    from app.core.config import get_config

    cfg = get_config()
    assert cfg.agent_vision_max_history_image_frames == 2
    assert cfg.agent_vision_max_current_turn_image_frames == 4


def test_legacy_jpeg_dimensions_estimate(tmp_path):
    jpg = _write_solid_jpeg(tmp_path / "legacy.jpg")
    env = tir.build_image_envelope("legacy", [{
        "mime": "image/jpeg",
        "data_b64": __import__("base64").b64encode(jpg.read_bytes()).decode("ascii"),
    }])
    est = estimate_tokens_rough([_tool_msg(env)])
    assert est > 2_000  # 1920x1080 should be ~2765, not the tiny JPEG byte ratio


def test_legacy_placeholder_path_from_text(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    env = tir.build_image_envelope("浏览器截图已保存：screenshot_abc.png（url=https://x/y）", [{
        "mime": "image/png",
        "data_b64": __import__("base64").b64encode(png.read_bytes()).decode("ascii"),
    }])
    ph = tir.image_envelope_placeholder(env)
    assert "saved at screenshot_abc.png" in ph


def test_legacy_placeholder_rejects_url():
    env = tir.build_image_envelope("see https://example.com/a/shot.png", [{
        "mime": "image/png",
        "data_b64": "QUJD",
    }])
    assert "saved at" not in tir.image_envelope_placeholder(env)


def test_telemetry_logs_prune(tmp_path, monkeypatch, caplog):
    import logging

    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)
    ]
    monkeypatch.setattr(tir, "_frame_limits", lambda: (2, 4))
    caplog.set_level(logging.INFO, logger="app.services.tool_image_results")
    tir.expand_tool_images(msgs)
    assert "image frames: total=5 selected=4 pruned=1 inlined=4" in caplog.text


def test_no_user_uses_current_limit(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [_tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
    }
    assert tir.select_kept_image_frames(
        msgs, parsed, history_limit=8, current_limit=2
    ) == {3, 4}


def test_interjection_does_not_move_boundary(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"f{i}"), call_id=f"f{i}") for i in range(5)
    ] + [
        {"role": "user", "content": "steer", "_interjection": True},
        _tool_msg(_envelope(png, "f5"), call_id="f5"),
    ]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
        if m.get("role") == "tool"
    }
    assert tir.select_kept_image_frames(
        msgs, parsed, history_limit=2, current_limit=4
    ) == {3, 4, 5, 7}


def test_compaction_summary_does_not_move_boundary(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"f{i}"), call_id=f"f{i}") for i in range(5)
    ] + [
        {"role": "user", "content": "[CONTEXT COMPACTION — REFERENCE ONLY] summary"},
        _tool_msg(_envelope(png, "f5"), call_id="f5"),
    ]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
        if m.get("role") == "tool"
    }
    assert tir.select_kept_image_frames(
        msgs, parsed, history_limit=2, current_limit=4
    ) == {3, 4, 5, 7}


def test_expand_uses_shipped_defaults(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)
    ]
    out = tir.expand_tool_images(msgs)
    assert out[1]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    for i in range(2, 6):
        assert isinstance(out[i]["content"], list)


def test_compression_shaves_images_more_aggressively(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(6)
    ]
    out, pruned = ContextCompressor(model="test", quiet=True)._prune_old_tool_results(msgs)
    assert pruned >= 4
    assert out[-1]["content"].startswith('{"_image_result"')


def test_total_frame_cap(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(10)
    ]
    parsed = {
        i: tir.parse_image_envelope(m.get("content"))
        for i, m in enumerate(msgs)
        if m.get("role") == "tool"
    }
    kept = tir.select_kept_image_frames(
        msgs, parsed, history_limit=8, current_limit=8
    )
    assert len(kept) == tir.MAX_TOTAL_INLINE_IMAGE_FRAMES


def test_total_b64_cap_drops_oldest(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(3)
    ]
    monkeypatch.setattr(tir, "MAX_INLINE_TOTAL_B64_CHARS", 100)
    out = tir.expand_tool_images(msgs)
    assert out[1]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    assert out[2]["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
    assert isinstance(out[3]["content"], list)


def test_legacy_decode_is_bounded(monkeypatch):
    import base64 as b64mod

    calls = []
    real = b64mod.b64decode

    def spy(data, *args, **kwargs):
        if isinstance(data, (bytes, bytearray)):
            calls.append(len(data))
        else:
            calls.append(len(str(data)))
        return real(data, *args, **kwargs)

    monkeypatch.setattr(tir.base64, "b64decode", spy)
    tir._image_size_from_b64("A" * 2_000_000)
    assert calls
    assert max(calls) <= tir.MAX_LEGACY_HEADER_B64_CHARS


def test_placeholder_strips_absolute_text_path():
    env = tir.build_image_envelope(
        "浏览器截图已保存：/root/weavethinker/backend/output_files/screenshot_abc.png（url=https://x）",
        [{"mime": "image/png", "data_b64": "QUJD"}],
    )
    ph = tir.image_envelope_placeholder(env)
    assert "saved at screenshot_abc.png" in ph
    assert "/root/" not in ph


def test_path_fallback_rejects_query_and_prose():
    query = tir.build_image_envelope(
        "see https://example.com/download?file=shot.png",
        [{"mime": "image/png", "data_b64": "QUJD"}],
    )
    prose = tir.build_image_envelope(
        "页面加载了 logo.png 但截图失败",
        [{"mime": "image/png", "data_b64": "QUJD"}],
    )
    assert "saved at" not in tir.image_envelope_placeholder(query)
    assert "saved at" not in tir.image_envelope_placeholder(prose)


def test_config_bad_values_clamped_and_missing_fallback():
    from app.core.config import get_config

    cfg = get_config()
    agent = cfg._config.setdefault("agent", {})
    old = agent.get("vision", None)
    try:
        agent["vision"] = {
            "max_history_image_frames": 99,
            "max_current_turn_image_frames": -3,
        }
        assert cfg.agent_vision_max_history_image_frames == 8
        assert cfg.agent_vision_max_current_turn_image_frames == 0

        agent["vision"] = {
            "max_history_image_frames": "bad",
            "max_current_turn_image_frames": None,
        }
        assert cfg.agent_vision_max_history_image_frames == 2
        assert cfg.agent_vision_max_current_turn_image_frames == 4

        agent.pop("vision", None)
        assert cfg.agent_vision_max_history_image_frames == 2
        assert cfg.agent_vision_max_current_turn_image_frames == 4
    finally:
        if old is None:
            agent.pop("vision", None)
        else:
            agent["vision"] = old


def test_select_without_parsed_map(tmp_path):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)
    ]
    assert len(tir.select_kept_image_frames(msgs)) == 4


def test_placeholder_strips_absolute_image_path_field():
    env = tir.build_image_envelope("shot", [{
        "mime": "image/png",
        "data_b64": "QUJD",
        "path": "/root/weavethinker/backend/output_files/shot.png",
    }])
    ph = tir.image_envelope_placeholder(env)
    assert "saved at shot.png" in ph
    assert "/root/" not in ph


def test_placeholder_rejects_scheme_url_in_trusted_prefix():
    env = tir.build_image_envelope(
        "浏览器截图已保存：https://evil.example.com/a/shot.png",
        [{"mime": "image/png", "data_b64": "QUJD"}],
    )
    ph = tir.image_envelope_placeholder(env)
    assert "saved at" not in ph
    # The prose itself may still mention the URL; that is not a saved-at claim.
    assert "saved at https://" not in ph


def test_relative_prose_path_is_not_mangled():
    text = tir._sanitize_placeholder_text("out/chart.png shown")
    assert "out/chart.png" in text


def test_kill_switch_compressor_placeholders_all(tmp_path, monkeypatch):
    png = _write_noise_png(tmp_path / "shot.png")
    msgs = [{"role": "user", "content": "go"}] + [
        _tool_msg(_envelope(png, f"s{i}"), call_id=f"c{i}") for i in range(5)
    ]
    monkeypatch.setattr(tir, "_frame_limits", lambda: (0, 0))
    out, pruned = ContextCompressor(model="test", quiet=True)._prune_old_tool_results(msgs)
    assert pruned == 5
    for msg in out[1:]:
        assert msg["content"].startswith(tir.IMAGE_PLACEHOLDER_PREFIX)
