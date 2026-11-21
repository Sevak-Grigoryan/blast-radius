"""pytest plugin: operand-type map of the pinned sqlglot's identity/equality comparisons.

Every single-operator `a is b`, `a is not b`, `a == b` and `a != b` in sqlglot/ is rewritten at
import time into a call that records (type(a), type(b)) for that site and returns the original
result. The map says which types reach each comparison while the whole suite runs, the same way
the coverage map says which tests reach each line. The selector uses it to reason about a patch:
`is` and `==` agree whenever both operands compare by identity.

    SQLGLOT_ROOT=$PWD PYTHONPATH=../tools python -m pytest tests -p typeprof -p no:cacheprovider
    -> $TYPEPROF_FILE (default /tmp/brcov/typeprof.json)
"""
import ast
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys

ROOT = os.path.abspath(os.environ.get("SQLGLOT_ROOT", "."))
PKG = os.path.join(ROOT, "sqlglot")
_seen = {}  # site -> set of (type, type)
OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}


def _br_cmp_(site, a, b, op):
    try:
        _seen.setdefault(site, set()).add((type(a), type(b)))
    except Exception:
        pass
    if op == 0:
        return a is b
    if op == 1:
        return a is not b
    if op == 2:
        return a == b
    return a != b


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(
            func=ast.Name("_br_cmp_", ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        tree = ast.parse(data, path)
        tree = _Rewrite(rel).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True)

    def exec_module(self, module):
        module.__dict__["_br_cmp_"] = _br_cmp_
        super().exec_module(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path or [ROOT])
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not os.path.abspath(spec.origin).startswith(PKG + os.sep):
            return spec
        spec.loader = _Loader(name, spec.origin)
        return spec


sys.dont_write_bytecode = True
sys.meta_path.insert(0, _Finder())

# process pools would run unprofiled interpreters: run their work inline
import concurrent.futures as _cf  # noqa: E402
import pickle as _pickle  # noqa: E402


def _rt(x):
    return _pickle.loads(_pickle.dumps(x))


class _InlineExecutor(_cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_rt(initargs))

    def submit(self, fn, /, *args, **kwargs):
        f = _cf.Future()
        try:
            args, kwargs = _rt((args, kwargs))
            f.set_result(_rt(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


_cf.ProcessPoolExecutor = _InlineExecutor


def _tname(t):
    return f"{t.__module__}.{t.__qualname__}"


def _identity_eq(t):
    """== and != on instances of t fall back to identity. None is a singleton whose own
    comparison returns NotImplemented for anything else, so it falls back to identity too."""
    if t is type(None):
        return True
    return t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__


def _owner(t, name):
    """The class whose `name` instances of t use (module.qualname)."""
    for c in t.__mro__:
        if name in vars(c):
            return _tname(c)
    return "?"


def _enum_info(t):
    import enum

    if isinstance(t, type) and issubclass(t, enum.Enum):
        # members are singletons with unique values; == compares values (mixin) or identity
        own = [k for k in ("__eq__", "__ne__") if any(k in vars(c) for c in t.__mro__
                                                       if c not in (object, enum.Enum, str, int))]
        return not own
    return False


def pytest_sessionfinish(session, exitstatus):
    types = {}
    sites = {}
    for site, pairs in _seen.items():
        out = []
        for a, b in pairs:
            for t in (a, b):
                n = _tname(t)
                if n not in types:
                    types[n] = {"id": _identity_eq(t), "enum": _enum_info(t),
                                "bool": t is bool, "none": t is type(None),
                                "eq": _owner(t, "__eq__"), "ne": _owner(t, "__ne__")}
            out.append([_tname(a), _tname(b)])
        sites[site] = sorted(out)
    out = os.environ.get("TYPEPROF_FILE", "/tmp/brcov/typeprof.json")
    w = os.environ.get("PYTEST_XDIST_WORKER")
    if w:  # one file per xdist worker; tools/build_typemap.py merges them
        out += "." + w
    with open(out, "w") as f:
        json.dump({"types": types, "sites": sites}, f, sort_keys=True)
