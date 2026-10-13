"""TDD — 用户头像处理（app/services/user_profile_service.py）。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_user_profile_service.py -v
"""
import base64
import io

import pytest
from PIL import Image

import app.services.user_profile_service as ups


def _img_bytes(w=512, h=512, fmt="PNG", mode="RGB", color=(200, 30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, (w, h), color).save(buf, format=fmt)
    return buf.getvalue()


class TestProcessAvatar:
    def test_png_becomes_jpeg_data_url_256(self):
        out = ups.process_avatar(_img_bytes())
        assert out.startswith("data:image/jpeg;base64,")
        raw = base64.b64decode(out.split(",", 1)[1])
        img = Image.open(io.BytesIO(raw))
        assert img.format == "JPEG"
        assert img.size == (256, 256)

    def test_non_square_center_cropped(self):
        out = ups.process_avatar(_img_bytes(w=800, h=400))
        raw = base64.b64decode(out.split(",", 1)[1])
        img = Image.open(io.BytesIO(raw))
        assert img.size == (256, 256)

    def test_palette_mode_converted(self):
        out = ups.process_avatar(_img_bytes(mode="P", color=3))
        raw = base64.b64decode(out.split(",", 1)[1])
        assert Image.open(io.BytesIO(raw)).mode == "RGB"

    def test_oversize_rejected(self):
        with pytest.raises(ValueError):
            ups.process_avatar(b"x" * (ups.MAX_UPLOAD_BYTES + 1))

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            ups.process_avatar(b"not an image at all")

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            ups.process_avatar(b"")

    def test_pixel_cap_declared(self):
        assert ups.MAX_PIXELS <= 40_000_000

    def test_oversized_pixels_rejected_before_decode(self, monkeypatch):
        """A4.9 复审修复：像素上限必须在解码前拦截——用 load() 间谍证明
        （旧时序 load→check 会让本测试失败；计时断言无鉴别力已弃用）。"""
        import PIL.Image as PILImage

        side = 5001  # 25,010,001 pixels > 25M
        buf = io.BytesIO()
        PILImage.new("L", (side, side), 128).save(buf, format="PNG")
        data = buf.getvalue()
        assert len(data) < ups.MAX_UPLOAD_BYTES

        called = {"load": False}
        orig_load = PILImage.Image.load

        def _spy(self, *args, **kwargs):
            called["load"] = True
            return orig_load(self, *args, **kwargs)

        monkeypatch.setattr(PILImage.Image, "load", _spy)
        with pytest.raises(ValueError):
            ups.process_avatar(data)
        assert called["load"] is False, "process_avatar decoded before the pixel cap check"
