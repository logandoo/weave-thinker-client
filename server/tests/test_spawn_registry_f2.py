"""F2 RED tests — 工具子进程登记与回收（spawn_registry）。

背景：terminal/code_execution 的后台子孙（`cmd &`）与 SIGKILL 后的会话子进程
无人回收 → 孤儿（60 生产实测 http.server 8099 常驻）。F2：三处 spawn 登记
pgid+cmdline 签名，关停回收 + 启动清扫；签名不匹配 fail-safe 不杀（只 prune）。

Run from backend/:  python -m pytest tests/test_spawn_registry_f2.py -v
"""
import asyncio
import json
import signal

from app.services import spawn_registry as sr


def _fresh_manifest(monkeypatch, tmp_path):
    path = tmp_path / "spawn_registry.json"
    monkeypatch.setattr(sr, "_manifest_path", lambda: path)
    return path


def _seed(path, pgid, cmdline, source="terminal"):
    data = {"entries": [{"pgid": pgid, "cmdline": cmdline, "cwd": "/tmp/ws",
                         "source": source, "ts": 0.0}]}
    path.write_text(json.dumps(data), encoding="utf-8")


def _entries(path):
    return json.loads(path.read_text(encoding="utf-8"))["entries"]


class TestRegister:
    def test_register_persists_and_unregister_removes(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        sr.register(777, "python3 -m http.server 8099", "/tmp/ws", "terminal")
        entries = _entries(path)
        assert entries and entries[0]["pgid"] == 777
        assert entries[0]["cmdline"] == "python3 -m http.server 8099"
        assert entries[0]["source"] == "terminal"
        sr.unregister(777)
        assert _entries(path) == []


class TestSweep:
    def test_sweep_kills_matching_group_and_prunes(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 777, "python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(800, 777, "python3 -m http.server 8099")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append((pgid, sig)))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.sweep_orphans())
        assert (777, signal.SIGTERM) in kills and (777, signal.SIGKILL) in kills
        assert res["killed"] == [777]
        assert _entries(path) == []

    def test_sweep_refuses_signature_mismatch(self, monkeypatch, tmp_path):
        """fail-safe：pgid 被无关进程复用时绝不误杀（HOST-SAFETY）。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 777, "python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(800, 777, "/usr/sbin/importantsvc --serve")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append((pgid, sig)))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.sweep_orphans())
        assert kills == []
        assert res["killed"] == []
        assert _entries(path) == []  # 陈旧条目 prune，但不杀

    def test_sweep_accepts_substring_signature(self, monkeypatch, tmp_path):
        """记录串与存活成员 cmdline 互为子串即匹配（bash -c 包装/后台子孙场景）。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 888, "/bin/bash -c python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(900, 888, "python3 -m http.server 8099")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append((pgid, sig)))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.sweep_orphans())
        assert res["killed"] == [888]

    def test_sweep_prunes_dead_entries(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 555, "some dead command")
        monkeypatch.setattr(sr, "_proc_table", lambda: [])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append((pgid, sig)))
        res = asyncio.run(sr.sweep_orphans())
        assert kills == []
        assert res["pruned"] == 1
        assert _entries(path) == []


class TestShutdown:
    def test_shutdown_terminates_live_matching_group(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 444, "python3 helper --serve", source="process")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(100, 444, "python3 helper --serve")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append((pgid, sig)))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.shutdown_all())
        assert res["killed"] == [444]
        assert (444, signal.SIGTERM) in kills


class TestA49FixRound:
    """评审 fix-round 边界（C1 严格签名 / 起始时间 / M2 竞态 / M3 坏条目 / M6 自组）。"""

    def test_short_token_member_never_matches(self, monkeypatch, tmp_path):
        """C1：'sh'/'python3' 等短 token 不得命中长记录（pgid 复用误杀面）。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 777, "bash -c python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(800, 777, "sh")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append(pgid))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.sweep_orphans())
        assert kills == [] and res["killed"] == []

    def test_exact_equality_matches_short_cmdline(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 666, "sleep 5")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(700, 666, "sleep 5")])
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append(pgid))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        res = asyncio.run(sr.sweep_orphans())
        assert res["killed"] == [666]

    def test_start_time_before_record_refuses(self, monkeypatch, tmp_path):
        """C1 防线：进程起始早于登记时刻 = pgid 复用/伪造 → 拒杀。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 777, "python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(800, 777, "python3 -m http.server 8099")])
        monkeypatch.setattr(sr, "_proc_start", lambda pid: 1.0)  # 远早于 ts=0.0+seed 后的实际 ts
        kills = []
        monkeypatch.setattr(sr, "_killpg", lambda pgid, sig: kills.append(pgid))
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)
        # seed ts=0.0 → 进程 start=1.0 >= 0.0+1? 边界：构造真正更早的场景
        import json as _json
        data = _json.loads(path.read_text())
        data["entries"][0]["ts"] = 1e9
        path.write_text(_json.dumps(data))
        res = asyncio.run(sr.sweep_orphans())
        assert kills == [] and res["killed"] == []

    def test_register_refuses_own_group(self, monkeypatch, tmp_path):
        """M6：永不登记服务自身进程组（start_new_session 回归时的灾难兜底）。"""
        import os as _os
        path = _fresh_manifest(monkeypatch, tmp_path)
        sr.register(_os.getpgid(0), "whatever", "/tmp", "terminal")
        assert (not path.exists()) or _entries(path) == []

    def test_sweep_preserves_concurrent_registration(self, monkeypatch, tmp_path):
        """M2：清扫等待期间新登记的条目必须幸存。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 777, "python3 -m http.server 8099")
        monkeypatch.setattr(sr, "_proc_table", lambda: [(800, 777, "python3 -m http.server 8099")])
        monkeypatch.setattr(sr, "_GRACE_SECONDS", 0.0)

        def killpg_and_register(pgid, sig):
            sr.register(999, "sleep 100", "/tmp", "process")

        monkeypatch.setattr(sr, "_killpg", killpg_and_register)
        res = asyncio.run(sr.sweep_orphans())
        assert res["killed"] == [777]
        left = _entries(path)
        assert [e["pgid"] for e in left] == [999]

    def test_corrupt_entry_does_not_break_register(self, monkeypatch, tmp_path):
        """M3：坏条目（非整型 pgid）不得阻断登记/清理。"""
        path = _fresh_manifest(monkeypatch, tmp_path)
        path.write_text('{"entries": [{"pgid": "bad", "cmdline": "x"}]}', encoding="utf-8")
        sr.register(555, "sleep 1", "/tmp", "terminal")
        pgs = [e["pgid"] for e in _entries(path)]
        assert 555 in pgs

    def test_unregister_if_empty_semantics(self, monkeypatch, tmp_path):
        path = _fresh_manifest(monkeypatch, tmp_path)
        _seed(path, 321, "sleep 9")
        monkeypatch.setattr(sr, "group_alive", lambda pgid: False)
        sr.unregister_if_empty(321)
        assert _entries(path) == []
        _seed(path, 321, "sleep 9")
        monkeypatch.setattr(sr, "group_alive", lambda pgid: True)
        sr.unregister_if_empty(321)
        assert len(_entries(path)) == 1  # 组内存活子孙 → 保留给清扫面
