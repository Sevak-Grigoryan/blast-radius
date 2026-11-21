"""Run a commit's candidate tests against the patched sqlglot, in a forked child.

The parent imports pytest once and never imports sqlglot. Each child installs an import hook
that serves the patched sources from memory, runs pytest on the candidate test ids with its
stdout sent to /dev/null, and reports which tests did not pass cleanly. Anything unexpected
(timeout, crash, a test that never reported) makes the caller fall back to the coverage set.
"""
import importlib.abc
import importlib.util
import json
import os
import resource
import select
import signal
import sys
import time

_PRELOADED = set()


def preload(heavy=False):
    """Import what every child needs (but not sqlglot) once in the parent, so forks start warm."""
    import importlib

    if "base" not in _PRELOADED:
        for name in ("unittest", "unittest.mock", "datetime", "decimal", "json", "csv", "gzip",
                     "pickle", "tracemalloc", "concurrent.futures", "multiprocessing", "typing",
                     "uuid", "textwrap", "dataclasses", "enum", "functools", "itertools",
                     "collections", "math", "re", "copy", "inspect", "string", "heapq", "zoneinfo"):
            try:
                importlib.import_module(name)
            except Exception:
                pass
        _PRELOADED.add("base")
    if heavy and "heavy" not in _PRELOADED:
        for name in ("numpy", "pandas", "pandas.testing", "duckdb"):
            try:
                importlib.import_module(name)
            except Exception:
                pass
        _PRELOADED.add("heavy")


class _Loader(importlib.abc.Loader):
    def __init__(self, filename, source):
        self.filename = filename
        self.source = source

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.source, self.filename, "exec", dont_inherit=True)
        exec(code, module.__dict__)

    def get_source(self, name):
        return self.source


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, patched):
        self.mods = {}
        for rel, src in patched.items():
            parts = rel[:-3].split("/")
            pkg = parts[-1] == "__init__"
            if pkg:
                parts = parts[:-1]
            self.mods[".".join(parts)] = (os.path.join(root, rel), src, pkg)

    def find_spec(self, name, path=None, target=None):
        e = self.mods.get(name)
        if e is None:
            return None
        fn, src, pkg = e
        return importlib.util.spec_from_file_location(
            name, fn, loader=_Loader(fn, src),
            submodule_search_locations=[os.path.dirname(fn)] if pkg else None,
        )


class _Collector:
    def __init__(self):
        self.bad = set()
        self.passed = set()
        self.dur = {}
        self.collect_failed = False

    def pytest_runtest_logreport(self, report):
        nid = report.nodeid
        if report.failed:
            self.bad.add(nid)
        if report.when == "call":
            if report.passed and getattr(report, "context", None) is None:
                self.passed.add(nid)
            self.dur[nid] = self.dur.get(nid, 0.0) + float(report.duration or 0.0)
        elif report.when == "setup":
            self.dur[nid] = self.dur.get(nid, 0.0) + float(report.duration or 0.0)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_failed = True


def _inline_pools():
    """Process pools would run the unpatched sqlglot in fresh interpreters: run inline instead,
    pickling arguments and results the way a real pool does."""
    import concurrent.futures as cf
    import pickle

    def rt(x):
        return pickle.loads(pickle.dumps(x))

    class Inline(cf.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                initializer(*rt(initargs))

        def submit(self, fn, /, *args, **kwargs):
            f = cf.Future()
            try:
                args, kwargs = rt((args, kwargs))
                f.set_result(rt(fn(*args, **kwargs)))
            except BaseException as e:  # noqa: BLE001
                f.set_exception(e)
            return f

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    cf.ProcessPoolExecutor = Inline


def _run_unittest(nodeids):
    """Run unittest test ids the way pytest's unittest support does; return (bad, started)."""
    import importlib
    import unittest

    bad, started, by_uid = set(), set(), {}
    suite = unittest.TestSuite()
    failed_mods = set()
    for nid in nodeids:
        path, cls, meth = nid.split("::")
        mod = path[:-3].replace("/", ".")
        by_uid[f"{mod}.{cls}.{meth}"] = nid
        if mod in failed_mods:
            bad.add(nid)
            continue
        try:
            m = importlib.import_module(mod)
            suite.addTest(getattr(m, cls)(meth))
        except BaseException:  # noqa: BLE001  import error fails every test of the file
            failed_mods.add(mod)
            bad.add(nid)

    def mark(test):
        uid = test.id()
        nid = by_uid.get(uid)
        if nid is not None:
            bad.add(nid)
            return
        # setUpClass / tearDownClass / module fixtures: blame every test of that class or module
        desc = str(test)
        for u, n in by_uid.items():
            owner = u.rsplit(".", 1)[0]
            if owner in desc or owner.rsplit(".", 1)[0] in desc.split("(")[-1]:
                bad.add(n)

    class R(unittest.TestResult):
        def startTest(self, test):
            started.add(test.id())
            super().startTest(test)

        def addError(self, test, err):
            mark(test)

        def addFailure(self, test, err):
            mark(test)

        def addSkip(self, test, reason):
            mark(test)

        def addUnexpectedSuccess(self, test):
            mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                mark(test)

    suite.run(R())
    for uid, nid in by_uid.items():
        if uid not in started:
            bad.add(nid)
    return bad


def _child(root, patched, nodeids, wfd):
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(root)
        os.environ["SKIP_INTEGRATION"] = "0"
        os.environ.setdefault("HOME", "/tmp")
        sys.path.insert(0, root)
        sys.meta_path.insert(0, _Finder(root, patched))
        sys.dont_write_bytecode = True
        _inline_pools()
        bad = _run_unittest(nodeids)
        data = json.dumps({"bad": sorted(bad)}).encode()
    except BaseException as e:  # noqa: BLE001
        data = json.dumps({"error": repr(e)}).encode()
    try:
        while data:
            n = os.write(wfd, data)
            data = data[n:]
    finally:
        os._exit(0)


def run(root, patched, nodeids, timeout):
    """Returns (set of node ids that did not pass, child cpu seconds) or (None, cpu) on any doubt."""
    sys.stdout.flush()
    sys.stderr.flush()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _child(root, patched, nodeids, w)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + timeout
    killed = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            killed = True
            break
        rd, _, _ = select.select([r], [], [], left)
        if rd:
            b = os.read(r, 1 << 16)
            if not b:
                break
            chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if killed:
        return None, cpu, "timeout"
    try:
        res = json.loads(b"".join(chunks))
    except Exception:
        return None, cpu, "no result"
    if "error" in res:
        return None, cpu, res["error"]
    return set(res["bad"]), cpu, ""


# ------------------------------------------------------------------ definition-time state
def _digest_factory():
    import enum
    import hashlib
    import re as _re
    import types

    def d(v, depth, seen):
        if v is None or isinstance(v, (bool, int, float, complex, str, bytes)):
            return type(v).__name__ + ":" + repr(v)
        if isinstance(v, enum.Enum):
            return "E:" + type(v).__qualname__ + "." + str(v.name)
        if isinstance(v, type):
            return "T:" + getattr(v, "__module__", "?") + "." + getattr(v, "__qualname__", "?")
        if isinstance(v, types.ModuleType):
            return "M:" + v.__name__
        if depth <= 0:
            return "…" + type(v).__name__
        i = id(v)
        if i in seen:
            return "↺"
        seen.add(i)
        try:
            if isinstance(v, (list, tuple)) and v and all(isinstance(x, type) for x in v):
                return type(v).__name__ + "(" + ",".join(sorted(d(x, depth - 1, seen) for x in v)) + ")"
            if isinstance(v, (list, tuple)):
                return type(v).__name__ + "(" + ",".join(d(x, depth - 1, seen) for x in v) + ")"
            if isinstance(v, dict):
                items = sorted(d(k, depth - 1, seen) + "=" + d(x, depth - 1, seen) for k, x in v.items())
                return type(v).__name__ + "{" + ",".join(items) + "}"
            if isinstance(v, (set, frozenset)):
                return type(v).__name__ + "{" + ",".join(sorted(d(x, depth - 1, seen) for x in v)) + "}"
            if isinstance(v, _re.Pattern):
                return "re:" + repr(v.pattern) + str(v.flags)
            if isinstance(v, (types.FunctionType, types.LambdaType)):
                parts = [v.__module__ or "", v.__qualname__]
                if v.__defaults__:
                    parts.append(d(v.__defaults__, depth - 1, seen))
                if v.__kwdefaults__:
                    parts.append(d(v.__kwdefaults__, depth - 1, seen))
                if v.__closure__:
                    for c in v.__closure__:
                        try:
                            parts.append(d(c.cell_contents, depth - 1, seen))
                        except ValueError:
                            parts.append("<empty>")
                return "F:" + "|".join(parts)
            if isinstance(v, (staticmethod, classmethod)):
                return type(v).__name__ + ":" + d(v.__func__, depth, seen)
            if isinstance(v, property):
                return "P:" + d(v.fget, depth, seen) + d(v.fset, depth, seen)
            if isinstance(v, (types.BuiltinFunctionType, types.MethodType, types.MethodDescriptorType,
                              types.WrapperDescriptorType, types.GetSetDescriptorType,
                              types.MemberDescriptorType)):
                return "B:" + getattr(v, "__qualname__", type(v).__name__)
            if isinstance(v, (types.GeneratorType, types.FrameType, types.CodeType)):
                return "G:" + type(v).__name__
            if hasattr(v, "__dict__") or hasattr(type(v), "__slots__"):
                st = {}
                if hasattr(v, "__dict__"):
                    st.update(vars(v))
                for cls in type(v).__mro__:
                    for s in getattr(cls, "__slots__", ()) or ():
                        if isinstance(s, str) and s not in st and hasattr(v, s):
                            try:
                                st[s] = getattr(v, s)
                            except Exception:
                                pass
                return "O:" + type(v).__module__ + "." + type(v).__qualname__ + d(st, depth - 1, seen)
            r = repr(v)
            if " at 0x" in r:
                return "X:" + type(v).__qualname__
            return "R:" + type(v).__qualname__ + r
        finally:
            seen.discard(i)

    def h(v):
        s = d(v, 7, set())
        return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()

    return h


def _snapshot():
    import importlib
    import pkgutil
    import types

    import sqlglot

    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except BaseException:  # noqa: BLE001
            pass
    h = _digest_factory()
    out = {}
    for mname, mod in sorted(sys.modules.items()):
        if not (mname == "sqlglot" or mname.startswith("sqlglot.")) or mod is None:
            continue
        for name, val in list(vars(mod).items()):
            if name.startswith("__") and name.endswith("__"):
                continue
            if isinstance(val, types.ModuleType):
                continue
            if isinstance(val, type) and getattr(val, "__module__", None) == mname:
                for a, v in list(vars(val).items()):
                    if a in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__",
                             "__firstlineno__", "__static_attributes__"):
                        continue
                    if isinstance(v, (types.FunctionType,)) and not v.__closure__ and not v.__defaults__:
                        continue
                    try:
                        out[f"{mname}:{val.__qualname__}.{a}"] = h(v)
                    except Exception:
                        out[f"{mname}:{val.__qualname__}.{a}"] = "?"
            elif isinstance(val, types.FunctionType) and val.__module__ == mname and not val.__closure__ and not val.__defaults__:
                continue
            else:
                try:
                    out[f"{mname}:{name}"] = h(val)
                except Exception:
                    out[f"{mname}:{name}"] = "?"
    return out


def _snap_child(root, patched, wfd):
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.chdir(root)
        sys.path.insert(0, root)
        sys.meta_path.insert(0, _Finder(root, patched))
        sys.dont_write_bytecode = True
        data = json.dumps({"snap": _snapshot()}).encode()
    except BaseException as e:  # noqa: BLE001
        data = json.dumps({"error": repr(e)}).encode()
    try:
        while data:
            n = os.write(wfd, data)
            data = data[n:]
    finally:
        os._exit(0)


def snapshot(root, patched, timeout=20):
    """Definition-time state of every sqlglot module, as {key: digest}, or None."""
    sys.stdout.flush()
    sys.stderr.flush()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        _snap_child(root, patched, w)
    os.close(w)
    chunks = []
    deadline = time.monotonic() + timeout
    killed = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            os.kill(pid, signal.SIGKILL)
            killed = True
            break
        rd, _, _ = select.select([r], [], [], left)
        if rd:
            b = os.read(r, 1 << 16)
            if not b:
                break
            chunks.append(b)
    os.close(r)
    os.waitpid(pid, 0)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    if killed:
        return None, cpu
    try:
        res = json.loads(b"".join(chunks))
    except Exception:
        return None, cpu
    return res.get("snap"), cpu
