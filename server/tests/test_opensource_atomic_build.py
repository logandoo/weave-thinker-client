"""回归：opensource_strip 原子构建（A4.9 评审 Important#3）。

事故形态（2026-10-31）：strip 被工具超时杀死，留下「已 wipe 的半成品树」被误提交。
修复：main() 先建 opensource.tmp/，成功才整目录换入；失败/被杀 → 旧树原样保留。
"""
import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "opensource_strip.py"


def _load():
    spec = importlib.util.spec_from_file_location("opensource_strip_mod", TOOL)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_atomic_build_preserves_old_tree_on_failure(tmp_path, monkeypatch):
    mod = _load()
    final = tmp_path / "opensource"
    final.mkdir()
    (final / "keep.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(mod, "DST", str(final))

    def boom():
        # 模拟「写到一半异常/被杀」：tmp 里已有半成品
        pathlib.Path(mod.DST).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(mod.DST) / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_build", boom)
    try:
        mod.main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("main() 必须把构建异常抛出")

    assert (final / "keep.txt").read_text(encoding="utf-8") == "old", \
        "构建失败时旧树必须原样保留"
    assert not (final / "partial.txt").exists(), "半成品不得进入正式树"
    assert not pathlib.Path(str(final) + ".tmp").exists(), "失败后不得残留 tmp"


def test_atomic_build_swaps_on_success(tmp_path, monkeypatch):
    mod = _load()
    final = tmp_path / "opensource"
    final.mkdir()
    (final / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(mod, "DST", str(final))

    def ok():
        pathlib.Path(mod.DST).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(mod.DST) / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(mod, "_build", ok)
    mod.main()

    assert (final / "new.txt").read_text(encoding="utf-8") == "new"
    assert not (final / "old.txt").exists(), "成功后必须是整目录换入（不是增量覆盖）"
    assert mod.DST == str(final), "main() 结束后 DST 必须指回正式树"
