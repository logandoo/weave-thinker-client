"""静态缓存头单元测试（2026-09-19）。

用户现象「office 预览全空白」根因之一=部署后浏览器继续执行陈旧 bundle
（index.html 无 Cache-Control → 启发式缓存/长开标签页）→ 内容哈希资源
immutable、index 及非哈希文件必须 revalidate。

Run: cd backend && python -m pytest tests/test_static_cache_headers.py -v
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from main import _static_cache_headers  # noqa: E402


IMMUTABLE = "public, max-age=31536000, immutable"


def test_hashed_assets_are_immutable():
    for name in (
        "assets/index-BX-672oa.js",
        "assets/useAsrStreaming-C3iij8z6.js",
        "assets/index-obQKJ4tA.js",
        "assets/katex-C1k2j3.woff2",
        "assets/ChatArea-C3MOaOR2.js",
        # base64url hashes may contain "-" (A4.9 fix6 Important-1) — these
        # exact names exist in the shipped build
        "assets/pdf.worker.min-Dswkl-cV.mjs",
        "assets/diagram-5BDNPKRD-sMiwj88-.js",
        "assets/rnnoise-LieH-clg.js",
        "assets/dagre-KV5264BT-CGs1k-Qh.js",
    ):
        assert _static_cache_headers(name)["Cache-Control"] == IMMUTABLE, name


def test_index_and_plain_files_revalidate():
    for name in (
        "index.html",
        "",
        "favicon.svg",
        "logo.png",
        "manifest.webmanifest",
        # outside assets/ → never pinned immutable, even with a hash-looking name
        "logo-2024.svg",
    ):
        cc = _static_cache_headers(name)["Cache-Control"]
        assert cc == "no-cache, must-revalidate", (name, cc)
