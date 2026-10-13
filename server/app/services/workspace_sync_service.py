"""云端 workspace → 本地物化原语（2026-09-29 波）。

背景：客户端本地栈读取 workspace 文件走本地路径；文件若只上传到云端
（另一设备/Web 端），本地缺失 → 预览 404。本模块提供「云端账户 workspace
文件同步」的纯逻辑底座：

- ``walk_remote`` 经远端既有 ``/api/files/list``（生产 chatbot 后端同款端点）
  递归构建文件清单（``None`` = 该目录远端不存在，不连坐）；跳过内部目录
  （.git/.venv/node_modules/…）与点文件；深度/目录数/条目数有界。
- ``should_pull`` 决定单个文件是否拉取：本地缺失，或远端 mtime 显著更新
  （LWW；本地较新的编辑不被旧远端覆盖）。下载后 mtime 对齐远端（调用方
  os.utime），使跨周期比较稳定、幂等。
- ``safe_target`` 是落点安全闸：只接受相对 rel_path，`..`/绝对路径/NUL 等
  一律拒绝；resolve 后必须在本地 workspace 内（符号链接逃逸同样拒绝）；
  允许指向尚不存在的文件（.part + 原子替换的写入目标）。

网络/落盘动作（下载流、鉴权、重试）留在 ``sync_client.SyncClient``；
本模块保持无 IO 副作用，便于单测（server/tests/test_workspace_sync_service.py）。
"""
from __future__ import annotations

import math
import os
import re
import stat as stat_module
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# 本应用生成的下载断点形状：`.<原文件名>.<8hex>.syncpart`（评审 Important：
# 不得误删用户自有的 `*.syncpart` 普通文件）
_SYNCPART_RE = re.compile(r"^\..+\.[0-9a-f]{8}\.syncpart$")

# 远端 mtime 可信窗口：接受 [0, now+86400] 的有限值，其余按缺失处理
# （评审 Important #3：远端可控 mtime 被 os.utime 原样落盘——inf/NaN 崩溃、
# 未来值钉死本地使后续远端更新永不生效；消毒后异常值退化为 size 比较）。
_MTIME_FUTURE_SLACK = 86400.0

# 远端递归清单的有界参数（workspace 是用户数据树，非无限深）。
MAX_WALK_DEPTH = 8
MAX_WALK_DIRS = 200
MAX_WALK_ENTRIES = 5000
_MAX_LISTING_ENTRIES = 2000  # 单目录条目迭代上限（防远端超高数组拖垮周期）
_MAX_PAGES_PER_DIR = 32  # 单目录页数上限（1000/页 → 3.2 万条；防坏服务端无进展死循环）

# 内部/派生物目录不参与 workspace 文件同步（与 workspace_scan._SKIP_DIRS 同族；
# uploads/scratch/noteimg/files/artifacts 是用户可见数据，必须同步）。
SKIP_DIR_NAMES = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".snapshots", ".cache", "tool_digests", "tool_results", "Library",
})

# fetch_list(rel_path, offset) -> listing dict | None（None=该目录远端不存在）。
# listing 新契约：{path, entries, truncated, total, offset, limit}；旧服务端无
# offset/total 字段（truncated=true 时只当截断告警，不翻页）。
ListFetcher = Callable[[str, int], Awaitable[Optional[Dict[str, Any]]]]


def is_relative_target(target: str) -> bool:
    """rel_path 形态校验：非空、非绝对、无控制字符（含 NUL/换行）、无反斜杠歧义。"""
    if not isinstance(target, str):
        return False
    text = target.strip()
    if not text or text != target:
        return False
    if _CTRL_CHARS.search(text):
        return False
    if text.startswith(("/", "\\")) or (len(text) >= 2 and text[1] == ":"):
        return False
    if "\\" in text:
        return False
    parts = text.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


_USER_WS_MARKER_RE = re.compile(r"/user_workspaces/[^/]+/(.+)$")


def normalize_workspace_path(target: Any) -> Optional[str]:
    """把旧附件里的远端绝对路径归一化为 workspace 相对路径（2026-09-29 波）。

    现场根因：旧消息附件的 ``rel_path`` 为 null，只有部署侧绝对路径
    ``/root/weavethinker/user_workspaces/<账号段>/<相对路径>``；本地预览与
    同步链路只认相对路径 → 404。归一规则：

    - 已是合法相对路径 → 原样返回；
    - 绝对路径（POSIX / Windows）含 ``/user_workspaces/<段>/`` → 取其后的
      相对部分，且必须仍为合法相对路径（含 ``..``/空 等 → None）；
    - 其余（无标记绝对路径 / 空 / 非字符串）→ None（调用方按 invalid 处理）。
    """
    if is_relative_target(target):
        return target
    if not isinstance(target, str):
        return None
    text = target.strip()
    if not text:
        return None
    # 仅绝对输入允许 marker 提取：相对形态的越权串（`../user_workspaces/…`）
    # 不因含 marker 而放行（评审 Minor：恢复严格拒绝语义）
    is_absolute = text.startswith(("/", "\\")) or (len(text) >= 2 and text[1] == ":")
    if not is_absolute:
        return None
    match = _USER_WS_MARKER_RE.search(text.replace("\\", "/"))
    if not match:
        return None
    rel = match.group(1)
    return rel if is_relative_target(rel) else None


def sanitize_remote_mtime(value: Any) -> Optional[float]:
    """远端 mtime 消毒：有限、非负、不超未来 24h；否则 None（退 size 比较）。"""
    try:
        mtime = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(mtime) or mtime < 0:
        return None
    if mtime > time.time() + _MTIME_FUTURE_SLACK:
        return None
    return mtime


def safe_target(root: str | Path, rel: str) -> Path:
    """把 rel_path 解析为本地 workspace 内的落点；越权/非法一律 ValueError。

    落点可以尚不存在（下载目标）；resolve() 会解析已存在的父链，符号链接
    父目录逃逸 workspace 时 containment 检查拒绝。
    """
    from app.services.workspace_paths import is_within

    if not is_relative_target(rel):
        raise ValueError(f"invalid workspace relative path: {rel!r}")
    root_path = Path(root).resolve()
    target = (root_path / rel).resolve()
    if not is_within(root_path, target):
        raise ValueError(f"target escapes workspace: {rel!r}")
    return target


def local_file_state(root: str | Path, rel: str) -> Dict[str, Any]:
    """本地落点状态（只认常规文件；非法路径/不存在均按缺席，不抛）。"""
    try:
        target = safe_target(root, rel)
        st = os.stat(target)
    except (ValueError, OSError):
        return {"exists": False, "size": None, "mtime": None}
    if not stat_module.S_ISREG(st.st_mode):
        return {"exists": False, "size": None, "mtime": None}
    return {"exists": True, "size": int(st.st_size), "mtime": float(st.st_mtime)}


def should_pull(remote: Dict[str, Any], local: Dict[str, Any]) -> bool:
    """远端文件是否应拉取到本地（LWW by mtime）。

    下载后本地 mtime 精确对齐远端（同一时钟域），因此比较用严格 `>`——
    远端更新（哪怕 +0.1s）必被拉取，相等=同一版本（幂等不重下）；同秒替换
    （mtime 相等但 size 不同）也拉。任一 mtime 缺失 → 退化为 size 比较。
    """
    if not local.get("exists"):
        return True
    r_mtime = remote.get("mtime")
    l_mtime = local.get("mtime")
    if r_mtime is None or l_mtime is None:
        return int(remote.get("size") or 0) != int(local.get("size") or 0)
    if float(r_mtime) > float(l_mtime):
        return True
    if float(r_mtime) == float(l_mtime):
        return int(remote.get("size") or 0) != int(local.get("size") or 0)
    return False


def _valid_entry_rel(rel: str) -> bool:
    parts = rel.split("/")
    return bool(parts) and all(p not in ("", ".", "..") for p in parts)


async def walk_remote(
    fetch_list: ListFetcher,
    *,
    max_depth: int = MAX_WALK_DEPTH,
    max_dirs: int = MAX_WALK_DIRS,
    max_entries: int = MAX_WALK_ENTRIES,
    on_truncated: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """递归远端清单 → 文件条目列表（rel_path/name/size/mtime/type）。

    ``fetch_list(rel, offset)`` 返回目录 listing 的一页（新契约含
    ``total/offset``；旧服务端无这些字段），或 ``None`` 表示远端该目录
    不存在（404）。其它异常向上传播（网络问题不应静默）。

    翻页（D-30）：``truncated=true`` 且响应含 ``offset/total`` → 按
    ``offset += 本页条目数`` 继续取下一页，直至 ``truncated=false`` 或到
    ``total``；单目录页数上限 ``_MAX_PAGES_PER_DIR``、全局条目上限
    ``max_entries``、单页迭代上限 ``_MAX_LISTING_ENTRIES`` 三重有界。
    旧服务端（无 ``offset`` 字段）不翻页，保留 ``on_truncated`` 告警。
    无进展（坏服务端声称分页但返回同一页）时立即终止并告警。
    """
    manifest: List[Dict[str, Any]] = []
    queue: List[tuple] = [(".", 0)]
    dirs_done = 0
    while queue and dirs_done < max_dirs and len(manifest) < max_entries:
        rel, depth = queue.pop(0)
        dirs_done += 1
        offset = 0
        pages = 0
        seen_rel: set = set()  # 本目录已见 rel_path（重复页检测，评审 Minor D）
        while pages < _MAX_PAGES_PER_DIR and len(manifest) < max_entries:
            listing = await fetch_list(rel, offset)
            pages += 1
            if listing is None:
                break
            page_entries = (listing.get("entries") or [])[:_MAX_LISTING_ENTRIES]
            page_new = 0  # 本页新增的 distinct rel_path 数（任意条目类型，含目录/点文件）
            for entry in page_entries:
                name = str(entry.get("name") or "")
                if not name:
                    continue
                erel = str(entry.get("rel_path") or (name if rel == "." else f"{rel}/{name}"))
                if not _valid_entry_rel(erel):
                    continue
                is_new = erel not in seen_rel
                if is_new:
                    seen_rel.add(erel)
                    page_new += 1
                else:
                    continue  # 同页/跨页重复条目（坏服务端）不重复处理
                if name.startswith("."):
                    continue  # 点文件/点目录不入清单也不入队（但已计 progress）
                if entry.get("is_dir"):
                    if name in SKIP_DIR_NAMES or depth + 1 > max_depth:
                        continue
                    if dirs_done + len(queue) < max_dirs:
                        queue.append((erel, depth + 1))
                else:
                    manifest.append({
                        "rel_path": erel,
                        "name": name,
                        "size": int(entry.get("size") or 0),
                        "mtime": sanitize_remote_mtime(entry.get("mtime")),
                        "type": entry.get("type"),
                    })
                    if len(manifest) >= max_entries:
                        break
            if listing.get("truncated") and page_new == 0:
                # 整页无任何新增条目（服务端重复同一页且回显一致）→ 无进展终止。
                # 注意按"任意条目"计 progress：整页子目录/点文件也合法（评审 R2
                # Minor：仅按文件计会把 dir-only 页误判为无进展而漏拉后续文件）
                if on_truncated is not None:
                    on_truncated(rel)
                break
            if len(manifest) >= max_entries:
                # 全局条目预算耗尽（评审 Minor）：可观测，不静默
                if on_truncated is not None:
                    on_truncated(rel)
                break
            if not listing.get("truncated"):
                break
            if "offset" not in listing or "total" not in listing:
                # 旧服务端：无分页字段，截断不可翻页 —— 告警一次即止
                if on_truncated is not None:
                    on_truncated(rel)
                break
            try:
                srv_offset = int(listing.get("offset"))
                total = int(listing.get("total") or 0)
                srv_limit = int(listing.get("limit") or 0)
            except (TypeError, ValueError):
                # 坏字段（评审 Minor）：按不可翻页处理，不拖垮整轮清单
                if on_truncated is not None:
                    on_truncated(rel)
                break
            if srv_offset != offset:
                # 回显偏移与请求不符（坏服务端）→ 终止，防错位/死循环
                if on_truncated is not None:
                    on_truncated(rel)
                break
            # 推进量：取"已发条目数"与"服务端页长"的较大者——兼容
            # 先切页后过滤的旧式实现（评审 Critical：按 len(entries) 推进会错位）。
            # 条目数超客户端单页上限时不按原始长度推进（未处理项不能跳过）。
            raw_len = len(listing.get("entries") or [])
            if raw_len > _MAX_LISTING_ENTRIES:
                advance = srv_limit if srv_limit > 0 else len(page_entries)
            else:
                advance = max(len(page_entries), srv_limit if srv_limit > 0 else 0)
            next_offset = offset + advance
            if advance <= 0 or (total and next_offset >= total):
                # 无进展/口径不一致：告警终止
                if on_truncated is not None:
                    on_truncated(rel)
                break
            offset = next_offset
        else:
            # 页数上限仍有 truncated：告警（单目录超过 _MAX_PAGES_PER_DIR 页）
            if on_truncated is not None:
                on_truncated(rel)
    return manifest


async def remote_workspace_stat(fetch_list: ListFetcher, rel: str) -> Dict[str, Any]:
    """远端单文件状态：父目录清单中精确命中（分页逐页查找）。

    - 命中 → ``exists=True`` + size/mtime；
    - 全页走完后确认不存在 → ``exists=False``；
    - 旧服务端截断后无法翻页，或坏服务端无进展 → ``exists=None``（未知，
      不代表不存在；调用方按需 fetch 应直接尝试下载，404 兜底）。
    """
    if not is_relative_target(rel):
        raise ValueError(f"invalid workspace relative path: {rel!r}")
    parent = rel.rsplit("/", 1)[0] if "/" in rel else "."
    offset = 0
    pages = 0
    while pages < _MAX_PAGES_PER_DIR:
        listing = await fetch_list(parent, offset)
        pages += 1
        if listing is None:
            return {"exists": False, "size": None, "mtime": None}
        page_entries = (listing.get("entries") or [])[:_MAX_LISTING_ENTRIES]
        for entry in page_entries:
            erel = str(entry.get("rel_path") or "")
            if erel == rel and not entry.get("is_dir"):
                return {
                    "exists": True,
                    "size": int(entry.get("size") or 0),
                    "mtime": sanitize_remote_mtime(entry.get("mtime")),
                }
        if not listing.get("truncated"):
            return {"exists": False, "size": None, "mtime": None}
        if "offset" not in listing or "total" not in listing:
            # 旧服务端截断不可翻页 → 未知
            return {"exists": None, "size": None, "mtime": None}
        try:
            srv_offset = int(listing.get("offset"))
            total = int(listing.get("total") or 0)
            srv_limit = int(listing.get("limit") or 0)
        except (TypeError, ValueError):
            return {"exists": None, "size": None, "mtime": None}
        raw_len = len(listing.get("entries") or [])
        if raw_len > _MAX_LISTING_ENTRIES:
            advance = srv_limit if srv_limit > 0 else len(page_entries)
        else:
            advance = max(len(page_entries), srv_limit if srv_limit > 0 else 0)
        next_offset = offset + advance
        if (srv_offset != offset or advance <= 0
                or (total and next_offset >= total)):
            # 回显不符 / 无进展 / 口径不一致（坏服务端）→ 未知
            return {"exists": None, "size": None, "mtime": None}
        offset = next_offset
    return {"exists": None, "size": None, "mtime": None}


def sweep_stale_syncparts(
    root: str | Path,
    ttl_seconds: float = 3600.0,
    max_depth: int = MAX_WALK_DEPTH,
    max_dirs: int = MAX_WALK_DIRS,
) -> int:
    """清扫 workspace 内陈旧的 ``*.syncpart``（崩溃残留下载断点），返回删除数。

    有界本地遍历（跳过点目录/SKIP_DIR_NAMES、深度与目录数上限）；只删
    mtime 早于 ``now - ttl_seconds`` 的常规文件（不跟符号链接）；新鲜残留
    与普通文件不动（活下载可能是另一周期正在写）。IO 异常逐个吞掉。
    """
    root_path = Path(root).resolve()
    cutoff = time.time() - float(ttl_seconds)
    removed = 0
    stack: List[tuple] = [(root_path, 0)]
    dirs_done = 0
    while stack and dirs_done < max_dirs:
        directory, depth = stack.pop()
        dirs_done += 1
        try:
            with os.scandir(directory) as it:
                children = list(it)
        except OSError:
            continue
        for child in children:
            name = child.name
            try:
                if child.is_dir(follow_symlinks=False):
                    if name.startswith(".") or name in SKIP_DIR_NAMES or depth >= max_depth:
                        continue
                    stack.append((Path(child.path), depth + 1))
                elif _SYNCPART_RE.match(name) and child.is_file(follow_symlinks=False):
                    if child.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(child.path)
                        removed += 1
            except OSError:
                continue
    return removed
