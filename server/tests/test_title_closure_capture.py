"""回归：chat.py 后台标题闭包（_generate_title_bg）加载的 _cap_* 变量必须在
Python 作用域链上可见——防止 2026-08-25 日志中的
`NameError: name '_cap_llm_service' is not defined`
（chat.py:1449 的绑定位于兄弟子树 _spawn_interrupted_save，对
chat_stream > event_generator > _run_tool_loop > _generate_title_bg 不可见）。

本测试做真实的嵌套作用域分析：只统计每个祖先函数「自身作用域」的绑定
（跳过其下嵌套 def/class/lambda 子树），不统计兄弟子树。
近似误差均为「偏紧」方向（import / AugAssign / 海象 / 元组 For 目标不计入绑定，
闭包自身嵌套子作用域内的 _cap_* 加载会被算入 loaded）→ 只可能误报失败,不会误包通过。

Run from backend/:  source ../.venv/bin/activate && python -m pytest tests/test_title_closure_capture.py -v
"""
import ast
from pathlib import Path

CHAT_PY = Path(__file__).resolve().parents[1] / "app" / "api" / "chat.py"
TRANSPORT_PY = Path(__file__).resolve().parents[1] / "app" / "api" / "chat_stream_transport.py"

_SCOPE_DEF = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _enclosing_chain(tree, lineno):
    """Return [f1, f2, ...] — every function node whose body contains lineno, outermost first."""
    stack = []

    def rec(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if child.lineno <= lineno <= child.end_lineno:
                    stack.append(child)
                    rec(child)
                    return

    rec(tree)
    return stack


def _own_scope_walk(node):
    """Yield nodes in node's own scope, skipping nested scope-def subtrees."""
    def walk(n):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, _SCOPE_DEF):
                continue
            yield child
            yield from walk(child)
    yield from walk(node)


def _store_names(nodes):
    names = set()
    for n in nodes:
        if isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            names.update(
                i.optional_vars.id for i in n.items
                if isinstance(i.optional_vars, ast.Name)
            )
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names


def _function_own_bound(func):
    names = _store_names(_own_scope_walk(func))
    a = func.args
    names.update(x.arg for x in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs))
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def test_cap_vars_loaded_in_title_closure_are_scope_visible():
    chat_tree = ast.parse(CHAT_PY.read_text())
    transport_tree = ast.parse(TRANSPORT_PY.read_text())
    target = next(
        f for f in ast.walk(transport_tree)
        if getattr(f, "name", None) == "_generate_title_bg"
    )
    chain = _enclosing_chain(transport_tree, target.lineno)
    assert chain and chain[-1] is target, "enclosing chain must end at the closure itself"

    loaded = {
        n.id for n in ast.walk(target)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
        and n.id.startswith("_cap_")
    }
    assert loaded, "guards against vacuous pass: no _cap_* loads found"

    visible = set()
    for func in chain[:-1]:
        visible |= _function_own_bound(func)
    # Module-scope names live in chat.py (the transport syncs them at call time).
    visible |= _store_names(_own_scope_walk(chat_tree))
    visible |= _store_names(_own_scope_walk(transport_tree))

    missing = sorted(loaded - visible)
    assert not missing, (
        f"_generate_title_bg loads _cap_* names not visible in its scope chain "
        f"{[n.name for n in chain]}: {missing} (loaded={sorted(loaded)})"
    )


if __name__ == "__main__":
    test_cap_vars_loaded_in_title_closure_are_scope_visible()
    print("PASS: all _cap_* loads in _generate_title_bg are scope-visible")
