"""工具图片回传 + 浏览器录像证据（P3/B4/B5）— A4.8 test-first。

覆盖：图片信封构造/展开为 content parts（data URL、文本+多图、非信封透传）；
workspace_read 图像分支（内联信封 / 关闭降级 vision_interpret 提示 / 超限拒）；
save_video 活体 webm 拷贝到 output_files（含 UNVERIFIED-audio 降级标记）；
video_status；mime/体积上限。
"""
import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import tool_image_results as tir  # noqa: E402

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_envelope_expand_roundtrip():
    env = tir.build_image_envelope("说明文字", [{"mime": "image/png", "data_b64": base64.b64encode(_PNG_1PX).decode()}])
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "t1", "name": "workspace_read", "content": env},
    ]
    out = tir.expand_tool_images(msgs)
    assert out[0] is msgs[0] or out[0] == msgs[0]  # 透传
    tool_msg = out[1]
    assert isinstance(tool_msg["content"], list)
    kinds = [p["type"] for p in tool_msg["content"]]
    assert kinds == ["text", "image_url"]
    assert tool_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert tool_msg["tool_call_id"] == "t1"


def test_expand_passes_through_plain_and_bad_json():
    msgs = [
        {"role": "tool", "content": '{"ok": true}'},
        {"role": "tool", "content": '{"_image_result": true, "images": ['},  # 坏 JSON
        {"role": "assistant", "content": "text"},
    ]
    out = tir.expand_tool_images(msgs)
    assert all(isinstance(m["content"], str) for m in out)


def test_mime_and_size_guard(tmp_path):
    assert tir.mime_for_path("a/b.PNG") == "image/png"
    assert tir.mime_for_path("a/b.txt") is None
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (tir.MAX_INLINE_BYTES + 1))
    assert tir.build_image_envelope_from_file(str(big), "x") is None
    ok = tmp_path / "ok.png"
    ok.write_bytes(_PNG_1PX)
    env = tir.build_image_envelope_from_file(str(ok), "x")
    assert env and json.loads(env)[tir.ENVELOPE_KEY] is True


def test_workspace_read_image_inline_and_degrade(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    png = ws / "probe.png"
    png.write_bytes(_PNG_1PX)

    async def _ensure(db, user_id, username=None):
        return SimpleNamespace(root_path=str(ws))

    monkeypatch.setattr("app.services.workspace_service.ensure_user_workspace", _ensure)

    from app.tools import workspace_read as wr

    user = SimpleNamespace(id="u1", username="tester")
    kwargs = {"user": user, "db": object()}

    async def scenario():
        r = json.loads(await wr.workspace_read({"path": "probe.png"}, **kwargs))
        assert r.get(tir.ENVELOPE_KEY) is True
        assert r["images"][0]["mime"] == "image/png"

        monkeypatch.setattr("app.services.tool_image_results.inline_images_enabled", lambda: False)
        r2 = json.loads(await wr.workspace_read({"path": "probe.png"}, **kwargs))
        assert not r2.get(tir.ENVELOPE_KEY)
        assert "vision_interpret" in r2.get("note", "")

    asyncio.run(scenario())


class _FakePage:
    url = "https://example.com"


class _FakeSession:
    def __init__(self, video_dir):
        self.video_dir = str(video_dir)
        self.page = _FakePage()
        self.last_activity = 0.0

    def touch(self):
        self.last_activity = 1.0

    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    _op_lock = _Lock()


def test_save_video_copies_live_webm(tmp_path):
    from app.services.interactive_browser_service import save_video, video_status

    vdir = tmp_path / "vid"
    vdir.mkdir()
    (vdir / "page1.webm").write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 100)
    (vdir / "page0.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    session = _FakeSession(vdir)

    result = asyncio.run(save_video(session, note="evidence"))
    assert result["success"] is True
    out_path = Path(result["path"])
    assert out_path.exists() and out_path.stat().st_size > 100  # 取较大的片段
    assert result["filename"].endswith(".webm")
    assert "UNVERIFIED-audio" in result["audio"]
    out_path.unlink()

    st = asyncio.run(video_status(session))
    assert st["recording"] is True and st["clips"] == 2


def test_save_video_without_recording(tmp_path):
    from app.services.interactive_browser_service import save_video

    empty = tmp_path / "none"
    empty.mkdir()
    r = asyncio.run(save_video(_FakeSession(empty)))
    assert r["success"] is False


def test_expand_rejects_spoof_and_honors_off_switch(monkeypatch):
    """A4.9 I2：非产图工具伪造信封不展开；超限图跳过；off 开关只回文本。"""
    env = tir.build_image_envelope("evil", [{"mime": "image/png", "data_b64": "QUJD"}])
    spoof = [{"role": "tool", "name": "terminal", "content": env}]
    out = tir.expand_tool_images(spoof)
    assert out[0]["content"] == env  # terminal 伪造 → 原样透传

    ok = [{"role": "tool", "name": "workspace_read", "content": env}]
    out2 = tir.expand_tool_images(ok)
    assert isinstance(out2[0]["content"], list)

    big = tir.build_image_envelope("big", [{"mime": "image/png", "data_b64": "A" * (tir.MAX_INLINE_BYTES * 2)}])
    out3 = tir.expand_tool_images([{"role": "tool", "name": "workspace_read", "content": big}])
    kinds = [p["type"] for p in out3[0]["content"]] if isinstance(out3[0]["content"], list) else []
    assert "image_url" not in kinds  # 展开期上限

    monkeypatch.setattr("app.services.tool_image_results.inline_images_enabled", lambda: False)
    out4 = tir.expand_tool_images([{"role": "tool", "name": "workspace_read", "content": env}])
    kinds4 = [p["type"] for p in out4[0]["content"]] if isinstance(out4[0]["content"], list) else []
    assert "image_url" not in kinds4  # off 开关
