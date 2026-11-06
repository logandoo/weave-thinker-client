#!/usr/bin/env python3
"""侧车 PyInstaller PYZ 内容探针（打包内容证据 L2 的内容腿）。

WHY: 侧车是 PyInstaller onedir，Python 字节码在 PYZ 归档内**压缩**存放
——直接对可执行体做字节扫必然 0 命中（恒真/恒假断言都是坏证据）。
本探针用 PyInstaller 归档读取器解出 PYZ，取目标模块的 code object，
递归收集字符串常量后做标记命中判定（语义面，非字节面）。

用法：python3 sidecar_pyz_probe.py <sidecar_exe> [marker ...]
输出：每模块一行 `<module>: marker=hit,marker=miss,...` + 模块总数。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

DEFAULT_MODULES = (
    "app.services.voice_ws",
    "app.services.voice_turn",
    "app.services.voice_tts",
    "app.services.voice_service",
    "app.services.voice_generation",
)
DEFAULT_MARKERS = (
    "audio_chunk",
    "epoch_v1",
    "turn_epoch",
    "speaking_start",
    "playback_paused",
    "playback_resumed",
)


def _string_consts(code, out: list[str]) -> list[str]:
    """收集模块里出现的**全部**字符串常量。

    注意（FCV 复核发现）：Python 里 ``{"turn_epoch": 1}`` 的键在 code object 的
    ``co_consts`` 中表现为**元组常量**（frozenset/tuple），不是 str —— 只收 str
    会把「以 dict key 形式出现」的标记误报成 miss（本波 `voice_turn.turn_epoch`
    即此形态）。故这里对 tuple/frozenset 递归取其元素，对其余常量取 str()。
    """
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _string_consts(const, out)
        elif isinstance(const, str):
            out.append(const)
        elif isinstance(const, (tuple, frozenset, list, set)):
            for item in const:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, (tuple, frozenset, list, set)):
                    for sub in item:
                        if isinstance(sub, str):
                            out.append(sub)
        elif isinstance(const, bytes):
            try:
                out.append(const.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    exe = argv[1]
    markers = argv[2:] or list(DEFAULT_MARKERS)
    if not os.path.isfile(exe):
        print(f"FAIL: sidecar executable missing: {exe}")
        return 1
    archive = CArchiveReader(exe)
    if "PYZ.pyz" not in archive.toc:
        print("FAIL: PYZ.pyz not found in CArchive")
        return 1
    tmp = tempfile.mktemp(suffix=".pyz")
    try:
        with open(tmp, "wb") as fh:
            fh.write(archive.extract("PYZ.pyz"))
        pyz = ZlibArchiveReader(tmp)
        voice_modules = sorted(m for m in pyz.toc if "voice" in m)
        print(f"PYZ voice modules: {len(voice_modules)}")
        for mod in voice_modules:
            print(f"  module: {mod}")
        print("--- 内容标记（code object 字符串常量面）---")
        for mod in DEFAULT_MODULES:
            if mod not in pyz.toc:
                print(f"{mod}: MISSING")
                continue
            consts = _string_consts(pyz.extract(mod), [])
            hits = [m for m in markers if any(m in c for c in consts)]
            misses = [m for m in markers if m not in hits]
            print(f"{mod}: hits={hits} misses={misses}")
        print("== MARKER_HITS ==")
        for mod in DEFAULT_MODULES:
            if mod not in pyz.toc:
                continue
            consts = _string_consts(pyz.extract(mod), [])
            for m in markers:
                if any(m in c for c in consts):
                    print(f"{m} {mod}")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
