"""pytest plugin: per-test coverage contexts (exact nodeid) + files opened per test."""
import os, sys, json
import coverage

ROOT = os.path.abspath(os.environ.get("SQLGLOT_ROOT", "."))
_cov = coverage.Coverage(data_file=os.environ.get("COV_FILE", "/tmp/brcov/.coverage"),
                         source=[os.path.join(ROOT, "sqlglot"), os.path.join(ROOT, "tests")],
                         branch=False)
_cov.start()

# Tests hand work to process pools (spawn); coverage cannot follow it there with the right
# test context, so run pool work in-process, synchronously, while mapping.
import concurrent.futures as _cf


import pickle as _pickle


def _rt(x):
    return _pickle.loads(_pickle.dumps(x))


class _InlineExecutor(_cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_rt(initargs))

    def submit(self, fn, /, *args, **kwargs):
        f = _cf.Future()
        try:
            # a real pool pickles arguments and results: keep that code path in the map
            args, kwargs = _rt((args, kwargs))
            f.set_result(_rt(fn(*args, **kwargs)))
        except BaseException as e:
            f.set_exception(e)
        return f

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


_cf.ProcessPoolExecutor = _InlineExecutor

_cur = {"id": "<import>"}
_opened = {}

def _audit(event, args):
    if event == "open":
        p = args[0]
        if isinstance(p, (str, bytes, os.PathLike)):
            try:
                p = os.fsdecode(p)
            except Exception:
                return
            ap = os.path.abspath(p)
            if ap.startswith(ROOT + os.sep) and not ap.endswith((".pyc",)):
                _opened.setdefault(_cur["id"], set()).add(os.path.relpath(ap, ROOT))
sys.addaudithook(_audit)

import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    # class/module-scoped setup (setUpClass, fixtures) runs here: give it its own context
    _cur["id"] = "<setup>" + item.nodeid
    _cov.switch_context("<setup>" + item.nodeid)
    # per-class caches filled on first use would credit only the first test with the lines
    # that fill them: start every test with them empty
    g = sys.modules.get("sqlglot.generator")
    if g is not None and hasattr(g, "_DISPATCH_CACHE"):
        g._DISPATCH_CACHE.clear()
    yield
    _cur["id"] = item.nodeid
    _cov.switch_context(item.nodeid)

def pytest_runtest_teardown(item, nextitem):
    _cur["id"] = "<between>"
    _cov.switch_context("<between>")

_types = {}


def _instrument():
    """Record, per test, which sqlglot classes get instantiated (class attributes are read at
    run time without executing any line, so coverage alone cannot see who depends on them)."""
    import sqlglot.expressions.core as core
    import sqlglot.dialects.dialect as dialect
    import sqlglot.parser as parser
    import sqlglot.generator as generator
    import sqlglot.tokens as tokens
    import sqlglot.schema as schema

    def wrap(cls):
        orig = cls.__init__

        def __init__(self, *a, **k):
            _types.setdefault(_cur["id"], set()).add(type(self))
            return orig(self, *a, **k)

        __init__.__wrapped__ = orig
        cls.__init__ = __init__

    for c in (core.Expr, dialect.Dialect, parser.Parser, generator.Generator, tokens.Tokenizer, schema.Schema):
        wrap(c)

    # dialect instances are cached: record every lookup too
    orig_get = dialect.Dialect.get_or_raise.__func__

    def get_or_raise(cls, d):
        r = orig_get(cls, d)
        _types.setdefault(_cur["id"], set()).add(type(r))
        return r

    dialect.Dialect.get_or_raise = classmethod(get_or_raise)


def pytest_configure(config):
    _instrument()


def _class_keys(types):
    keys = set()
    for t in types:
        for k in t.__mro__:
            mod = sys.modules.get(k.__module__)
            f = getattr(mod, "__file__", None)
            if f and os.path.abspath(f).startswith(ROOT + os.sep):
                keys.add(os.path.relpath(os.path.abspath(f), ROOT) + "::" + k.__qualname__)
    return sorted(keys)


def pytest_sessionfinish(session, exitstatus):
    _cov.stop()
    _cov.save()
    with open(os.environ.get("OPEN_FILE", "/tmp/brcov/opened.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _opened.items()}, f)
    with open(os.environ.get("TYPES_FILE", "/tmp/brcov/types.json"), "w") as f:
        json.dump({k: _class_keys(v) for k, v in _types.items()}, f)
