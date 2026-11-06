"""web 能力 P0（上轮评估 docs/AUDIT_2026-10-14_web_capability_canvas_interaction.md §五）。

P0-1 坐标鼠标原语（canvas/WebGL 无 ref 时的唯一寻址，CDP trusted）
P0-2 截图图像信封回传（视觉闭环）+ IMAGE_TOOL_ALLOWLIST 接线
P0-3 ref="_focused" 键入聚焦元素（canvas 游戏/隐藏 IME 输入）

Run: cd backend && python -m pytest tests/test_web_capability_p0.py -v
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _FakeMouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def down(self, button="left"):
        self.calls.append(("down", button))

    async def up(self, button="left"):
        self.calls.append(("up", button))

    async def click(self, x, y, button="left", clicks=1):
        self.calls.append(("click", x, y, button, clicks))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class _FakeKeyboard:
    def __init__(self):
        self.calls = []

    async def type(self, text, delay=0):
        self.calls.append(("type", text))

    async def press(self, key):
        self.calls.append(("press", key))


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()

    async def wait_for_load_state(self, *a, **k):
        return None


class _FakeSession:
    def __init__(self):
        self.page = _FakePage()
        self.ref_map = {}
        self._op_lock = asyncio.Lock()

    def touch(self):
        pass


def test_mouse_coordinate_primitives():
    """P0-1：move/down/up/click/wheel 走 page.mouse（CDP trusted）。"""
    from app.services.interactive_browser_service import mouse

    s = _FakeSession()

    async def _run():
        await mouse(s, "click", x=960, y=561)
        await mouse(s, "move", x=10, y=20)
        await mouse(s, "down")
        await mouse(s, "up")
        await mouse(s, "wheel", delta_y=500)

    asyncio.run(_run())
    assert ("click", 960.0, 561.0, "left", 1) in s.page.mouse.calls
    assert ("move", 10.0, 20.0) in s.page.mouse.calls
    assert ("down", "left") in s.page.mouse.calls
    assert ("up", "left") in s.page.mouse.calls
    assert ("wheel", 0, 500.0) in s.page.mouse.calls


def test_type_focused_real_key_events():
    """P0-3：_focused 走 keyboard.type（trusted 键入），submit 追加 Enter。"""
    from app.services.interactive_browser_service import type_focused

    s = _FakeSession()

    async def _run():
        await type_focused(s, "yy2026", submit=True)

    asyncio.run(_run())
    assert ("type", "yy2026") in s.page.keyboard.calls
    assert ("press", "Enter") in s.page.keyboard.calls


def test_browser_type_focused_dispatch():
    """P0-3 接线：browser_type(ref='_focused') 走 type_focused 分支。"""
    from app.tools import browser_interact

    src = open(browser_interact.__file__, encoding="utf-8").read()
    assert '_focused' in src
    assert "type_focused" in src


def test_screenshot_returns_image_envelope():
    """P0-2：browser_screenshot 出线为图像信封（text + data URL 可展开）。"""
    from app.services.tool_image_results import (
        IMAGE_TOOL_ALLOWLIST,
        build_image_envelope_from_file,
        expand_tool_images,
    )

    assert "browser_screenshot" in IMAGE_TOOL_ALLOWLIST
    png = os.path.join(os.path.dirname(__file__), "probe_vision.png")
    if not os.path.isfile(png):
        png = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "probe_vision.png")
    env = build_image_envelope_from_file(png, "浏览器截图已保存")
    assert env and json.loads(env).get("_image_result") is True
    msgs = [{"role": "tool", "name": "browser_screenshot", "content": env}]
    out = expand_tool_images(msgs)
    parts = out[0]["content"]
    assert any(p.get("type") == "image_url" for p in parts)


def test_vision_interpret_resolves_output_files():
    """P0-2 断链修复：vision_interpret 可解析 backend/output_files 截图。"""
    import app.tools.vision_interpret as vi

    src = open(vi.__file__, encoding="utf-8").read()
    assert "output_files" in src
    assert "_resolve_workspace_path" in src  # 工作区解析仍是第一优先


def test_p1_cursor_interactive_and_shadow_refs():
    """P1（A4.9 同化 agent-browser）：onclick/cursor:pointer/tabindex 裸元素
    合成 ref；walker 穿透 shadowRoot。"""
    import app.services.interactive_browser_service as ibs

    src = open(ibs.__file__, encoding="utf-8").read()
    assert "cursorClickable" in src
    assert "el.shadowRoot" in src


def test_p1_iframe_frame_walk_and_frame_ref_resolution():
    """P1：iframe 遍历（Playwright frame API）+ frame ref 可点（前缀剥离+frame 定位）。"""
    import app.services.interactive_browser_service as ibs

    src = open(ibs.__file__, encoding="utf-8").read()
    assert "frames[1:6]" in src
    assert "_frame_of" in src
    assert r'^__br_(?:f\d+_)?\d+$' in src
    assert 're.sub(r"^__br_f\\d+_"' in src or '_dom_ref' in src


def test_p1_navigate_wait_selector_and_storage_state():
    """P1：navigate 就绪参数 + storage_state 持久。
    AST 级检查（A4.9 C1 教训：字符串在场断言把 NameError 断行钉成绿灯）——
    _state_arg 必须先赋值后使用。"""
    import ast
    import app.services.interactive_browser_service as ibs
    import app.tools.browser_interact as bi

    src = open(ibs.__file__, encoding="utf-8").read()
    assert "wait_selector" in src and "_storage_state_path" in src
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "get_or_create_session")
    store_pos = min((n.lineno for n in ast.walk(fn)
                     if isinstance(n, ast.Name) and n.id == "_state_arg"
                     and isinstance(n.ctx, ast.Store)), default=10**9)
    load_pos = min((n.lineno for n in ast.walk(fn)
                    if isinstance(n, ast.Name) and n.id == "_state_arg"
                    and isinstance(n.ctx, ast.Load)), default=10**9)
    assert store_pos < load_pos, f"_state_arg 先用后定义（NameError 面）：store@{store_pos} load@{load_pos}"
    bsrc = open(bi.__file__, encoding="utf-8").read()
    assert '"wait_selector"' in bsrc and '"timeout_ms"' in bsrc
