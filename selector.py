"""Blast Radius selector: pick the tests a patch can break, from a coverage map of the pinned sqlglot.

For each patch:
  1. Python files: rebuild the patched file, diff the token streams of old and new (comments and
     blank lines ignored), and turn every changed token into lines of the pinned file.
  2. Each changed line -> the tests that executed it (coverage map). Lines run only at import
     time (class bodies, decorators, module constants) -> the tests that use their enclosing class,
     function or module. A file that no longer compiles -> every test that can import it.
  3. Data files (fixtures) -> the tests that opened them.
  4. Split the union over 1..8 workers (longest first) and keep the cheapest split.
"""
import ast
import bisect
import difflib
import gzip
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SQLGLOT_SRC", HERE / "sqlglot"))
MAP = HERE / "map.json.gz"
WORKER_MS = 2000

M = json.loads(gzip.open(MAP).read())
SECONDS = M["tests"]
MS = [round(s * 1000) for s in SECONDS]
N = len(SECONDS)
SETS = M["sets"]
ALL = set(range(N))
TEST_IDS = M.get("ids", [])
try:
    IMPLINES = {f: set(v) for f, v in json.loads(gzip.open(HERE / "implines.json.gz").read()).items()}
except Exception:
    IMPLINES = None  # unknown: every function line could run at import time
INFO = {}


def log(*a):
    print(*a, file=sys.stderr)


# ------------------------------------------------------------------ patch parsing
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> list of (old_path, new_path, hunks); hunk = (old_start, [(tag, line)])"""
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "header": l}
            files.append(cur)
        elif cur is not None and l.startswith("--- ") and not cur["hunks"]:
            p = l[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif cur is not None and l.startswith("+++ ") and not cur["hunks"]:
            p = l[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif cur is not None and l.startswith("@@"):
            m = HUNK.match(l)
            if m:
                cur["hunks"].append([int(m.group(1)), []])
        elif cur is not None and cur["hunks"] and l[:1] in (" ", "-", "+"):
            cur["hunks"][-1][1].append((l[0], l[1:]))
        elif cur is not None and cur["hunks"] and l.startswith("\\"):
            pass  # "\ No newline at end of file"
        elif cur is not None and l.startswith("rename from "):
            cur["old"] = l[len("rename from "):]
        elif cur is not None and l.startswith("rename to "):
            cur["new"] = l[len("rename to "):]
        i += 1
    for f in files:
        if f["old"] is None and f["new"] is None:
            parts = f["header"].split(" ")
            if len(parts) >= 4:
                f["old"] = parts[2][2:]
                f["new"] = parts[3][2:]
    return files


def apply_hunks(old_lines, hunks):
    out = []
    pos = 0  # index into old_lines
    for start, body in hunks:
        s = max(start - 1, 0) if body and any(t != "+" for t, _ in body) else start
        out.extend(old_lines[pos:s])
        pos = s
        for tag, text in body:
            if tag == " ":
                out.append(old_lines[pos] if pos < len(old_lines) else text)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(text)
    out.extend(old_lines[pos:])
    return out


def changed_lines_naive(hunks):
    """Old-file line numbers touched by a hunk (deleted lines, and the neighbours of insertions)."""
    res = set()
    for start, body in hunks:
        line = start
        prev_ctx = start - 1
        for tag, _ in body:
            if tag == " ":
                prev_ctx = line
                line += 1
            elif tag == "-":
                res.add(line)
                prev_ctx = line
                line += 1
            else:
                res.add(max(prev_ctx, 1))
                res.add(line)
    return res


# ------------------------------------------------------------------ python token diff
SKIP = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}


def toks(src):
    out = []
    for t in tokenize.generate_tokens(io.StringIO(src).readline):
        if t.type in SKIP:
            continue
        out.append((t.type, t.string, t.start[0], t.end[0]))
    return out


def changed_lines_py(old_src, new_src):
    """Old lines whose code changed (comments ignored). None if the token diff is unavailable."""
    try:
        a = toks(old_src)
        b = toks(new_src)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    ka = [(t[0], t[1]) for t in a]
    kb = [(t[0], t[1]) for t in b]
    # common prefix / suffix first: the edit is usually small, the file large
    p = 0
    n = min(len(ka), len(kb))
    while p < n and ka[p] == kb[p]:
        p += 1
    q = 0
    while q < n - p and ka[len(ka) - 1 - q] == kb[len(kb) - 1 - q]:
        q += 1
    ma, mb = ka[p:len(ka) - q], kb[p:len(kb) - q]
    if len(ma) * len(mb) > 4_000_000:
        return None  # too large to align cheaply: fall back to the hunks' lines
    sm = difflib.SequenceMatcher(None, ma, mb, autojunk=False)
    res = set()
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        i1 += p
        i2 += p
        if op == "equal":
            continue
        if i2 > i1:
            for t in a[i1:i2]:
                res.update(range(t[2], t[3] + 1))
        else:
            # pure insertion before token i1: the lines around it
            if i1 > 0:
                res.add(a[i1 - 1][3])
            if i1 < len(a):
                res.add(a[i1][2])
    return res


# ------------------------------------------------------------------ line -> tests
_stmt_cache = {}


def stmt_starts(rel, src):
    """Sorted (start, end) of every statement/expression node, for mapping continuation lines."""
    if rel in _stmt_cache:
        return _stmt_cache[rel]
    spans = []
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt) and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                spans.append((node.lineno, node.end_lineno))
    except SyntaxError:
        pass
    _stmt_cache[rel] = spans
    return spans


def _set(i):
    return set(SETS[i]) if i is not None else set()


def _classes_in(rel):
    return [sp for sp in M["scopes"].get(rel, []) if len(sp) > 3 and sp[3] == "c"]


def _class_users(rel, qual):
    return _set(M["classusers"].get(rel + "::" + qual))


LAZY = [i for i, t in enumerate(TEST_IDS) if t.endswith("::test_lazy_load")]


def scope_users(rel, line, runtime_dead=False):
    """Tests that may depend on code at `line` that coverage does not attribute to a test:
    class bodies (attributes read at run time), decorators, defaults, module constants."""
    best = None
    for sp in M["scopes"].get(rel, []):
        a, b = sp[0], sp[1]
        if a <= line <= b and (best is None or (b - a) < (best[1] - best[0])):
            best = sp
    sel = set()
    if best is not None:
        sel |= _set(best[2])
        if len(best) > 3 and best[3] == "c":
            sel |= _class_users(rel, best[4])
        elif len(best) > 3:
            # a function: also whoever uses its class, if it is a method
            q = best[4]
            if "." in q and not runtime_dead:
                sel |= _class_users(rel, q.rsplit(".", 1)[0])
        if sel or runtime_dead:
            return sel
    if runtime_dead:
        return sel
    # module level: users of this module, of its classes, and of the modules importing it
    sel |= _set(M["users"].get(rel))
    for sp in _classes_in(rel):
        sel |= _class_users(rel, sp[4])
    for g in M["importers"].get(rel, []):
        sel |= _set(M["users"].get(g))
        for sp in _classes_in(g):
            sel |= _class_users(g, sp[4])
    if rel.startswith("sqlglot/"):
        sel |= set(LAZY)
    return sel if sel else set(ALL)


def module_users(rel):
    """Users of a module, of its classes, and of the modules importing it."""
    sel = _set(M["users"].get(rel))
    for sp in _classes_in(rel):
        sel |= _class_users(rel, sp[4])
    for g in M["importers"].get(rel, []):
        sel |= _set(M["users"].get(g))
        for sp in _classes_in(g):
            sel |= _class_users(g, sp[4])
    if rel.startswith("sqlglot/"):
        sel |= set(LAZY)
    return sel if sel else set(ALL)


def import_time_body(rel, line):
    """A line inside a function (or lambda) body that runs while sqlglot is being imported:
    a metaclass, __init_subclass__, a builder called at module level, a parse of a constant."""
    if IMPLINES is not None and line not in IMPLINES.get(rel, ()):
        return False
    sc = innermost(rel, line)
    if sc is not None and len(sc) > 5 and sc[3] == "f" and line >= sc[5]:
        return True
    return in_lambda(rel, line)


def _mod_file(mod):
    base = mod.replace(".", "/")
    if (SRC / (base + ".py")).exists():
        return base + ".py"
    if (SRC / base / "__init__.py").exists():
        return base + "/__init__.py"
    return None


def definition_users(changed_keys):
    """Tests that depend on module constants or class attributes whose import-time value changed."""
    sel = set()
    for key in changed_keys:
        mod, _, name = key.partition(":")
        rel = _mod_file(mod)
        if rel is None:
            return set(ALL)
        if "." in name:
            qual = name.rsplit(".", 1)[0]
            sp = next((sp for sp in _classes_in(rel) if sp[4] == qual), None)
            if sp is None:
                sel |= module_users(rel)
                continue
            sel |= _set(sp[2]) | _class_users(rel, qual)
        else:
            sel |= module_users(rel)
    return sel


def innermost(rel, line):
    best = None
    for sp in M["scopes"].get(rel, []):
        if sp[0] <= line <= sp[1] and (best is None or (sp[1] - sp[0]) < (best[1] - best[0])):
            best = sp
    return best


def in_lambda(rel, line):
    return any(a <= line <= b for a, b in M["lambdas"].get(rel, []))


def tests_for_py_lines(rel, src, lines):
    lmap = M["lines"].get(rel)
    if lmap is None:
        # a Python file no test ever ran (scripts, docs, the C build): nothing imports it
        return set()
    imp = set(M["imp"].get(rel, []))
    sel = set()

    def runtime(L):
        """Tests that executed L, or earlier lines of its statement (short-circuits)."""
        out = set()
        cands = [L]
        inner = None
        for a, b in stmt_starts(rel, src):
            if a <= L <= b and (inner is None or b - a < inner[1] - inner[0]):
                inner = (a, b)
        if inner is not None:
            cands.extend(range(inner[0], L))
        hit = False
        for c in cands:
            k = str(c)
            if k in lmap:
                out |= set(SETS[lmap[k]])
                hit = True
            if c in imp:
                hit = True
        return out, hit

    for L in lines:
        sp = innermost(rel, L)
        rt, hit = runtime(L)
        sel |= rt
        if in_lambda(rel, L):
            # inside a multi-line lambda: run-time code, the coverage above is exact
            if not hit:
                sel |= scope_users(rel, L, runtime_dead=True)
            continue
        if sp is None or sp[3] == "c":
            # module or class body: runs once, at import (possibly lazily inside some test);
            # whoever uses the class / module may depend on it
            sel |= scope_users(rel, L)
        elif L < sp[5]:
            # a function header (decorators, signature, defaults): whoever calls the function
            sel |= set(SETS[sp[2]])
            if not SETS[sp[2]]:
                sel |= scope_users(rel, L)
        elif not hit:
            # function body never executed: only the function's own users could reach it
            sel |= scope_users(rel, L, runtime_dead=True)
    return sel


# ------------------------------------------------------------------ one patch
def select(patch_text):
    sel = set()
    INFO.clear()
    INFO["defs"] = set()      # files whose import-time function lines changed
    INFO["py"] = {}           # patched sqlglot sources
    for f in parse_patch(patch_text):
        rel = f["old"] or f["new"]
        new_rel = f["new"]
        if rel is None:
            continue
        if f["old"] is None:
            # a new file: only matters if something opens or imports it, which the pinned
            # tests cannot do except through package discovery -> nothing
            if new_rel and new_rel.endswith(".py") and new_rel.startswith("sqlglot/"):
                sel |= set(M["users"].get("sqlglot/__init__.py") and SETS[M["users"]["sqlglot/__init__.py"]] or [])
            continue
        if rel.endswith(".py"):
            p = SRC / rel
            if not p.exists():
                continue
            old_src = p.read_text()
            if f["new"] is None:
                # deleted module: everyone who used it, and everyone if it is part of the package
                sel |= set(SETS[M["users"][rel]]) if rel in M["users"] else set()
                if rel.startswith("sqlglot/"):
                    sel |= ALL
                continue
            old_lines = old_src.split("\n")
            new_src = "\n".join(apply_hunks(old_lines, f["hunks"]))
            try:
                compile(new_src, rel, "exec")
            except SyntaxError:
                if rel.startswith("tests/"):
                    fs = M["files_of_tests"].get(rel)
                    sel |= set(SETS[fs]) if fs is not None else set()
                    sel |= set(SETS[M["users"][rel]]) if rel in M["users"] else set()
                else:
                    sel |= ALL
                continue
            lines = changed_lines_py(old_src, new_src)
            if lines is None:
                lines = changed_lines_naive(f["hunks"])
            if not lines:
                continue  # only comments / blank lines changed
            sel |= tests_for_py_lines(rel, old_src, lines)
            if rel.startswith("sqlglot/"):
                INFO["py"][rel] = new_src
                for L in lines:
                    if import_time_body(rel, L):
                        INFO["defs"].add(rel)
                        break
        else:
            o = M["opened"].get(rel)
            if o is not None:
                sel |= set(SETS[o])
    return sel


def split(sel):
    """Longest first onto the least loaded worker, for 1..8 workers; keep the cheapest."""
    if not sel:
        return []
    order = sorted(sel, key=lambda t: -MS[t])
    best = None
    for k in range(1, 9):
        if k > len(order):
            break
        loads = [0] * k
        work = [[] for _ in range(k)]
        for t in order:
            i = loads.index(min(loads))
            loads[i] += MS[t]
            work[i].append(t)
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, work)
    return best[1]


def load_numbering():
    """Map the coverage map's test indexes onto /data/tests.json (normally identical)."""
    global MS
    try:
        real = json.loads((DATA / "tests.json").read_text())["tests"]
    except Exception as e:
        log(f"tests.json unreadable ({e!r}); using the map's numbering")
        return None, []
    ids = M["ids"]
    if [t["id"] for t in real] == ids:
        MS = [round(t["seconds"] * 1000) for t in real]
        return None, []
    log("tests.json differs from the map: remapping by id")
    by_id = {t["id"]: t["number"] for t in real}
    remap = {i: by_id[tid] for i, tid in enumerate(ids) if tid in by_id}
    known = set(ids)
    unknown = [t["number"] for t in real if t["id"] not in known]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in real}
    MS = [0] * (max(ms) + 1 if ms else 0)
    for k, v in ms.items():
        MS[k] = v
    return remap, unknown


# ------------------------------------------------------------------ run-time check
# Running sqlglot's tests while deciding is allowed and its CPU time is charged. When the
# coverage set is cheap to run, run it against the patched code: if nothing fails the commit
# needs no worker at all, and if something fails only the failing tests are kept.
P_PASS = 0.5          # chance that a coverage set of a code change has no failing test
CPU_BUDGET = 40.0     # seconds of child CPU for the whole batch
WALL_BUDGET = 75.0    # seconds since start after which no new check starts
T0 = None


def patched_sources(patch_text):
    """{relpath: new source} when every changed file is an existing .py under sqlglot/ or tests/."""
    out = {}
    for f in parse_patch(patch_text):
        old, new = f["old"], f["new"]
        if old is None or new is None or old != new or not old.endswith(".py"):
            return None
        if not (old.startswith("sqlglot/") or old.startswith("tests/")):
            return None
        p = SRC / old
        if not p.exists():
            return None
        src = "\n".join(apply_hunks(p.read_text().split("\n"), f["hunks"]))
        if "subprocess" in src:
            return None
        try:
            compile(src, old, "exec")
        except SyntaxError:
            return None
        out[old] = src
    return out or None


_UNVERIFIABLE = None


def unverifiable_ids():
    """Tests that start another interpreter: it would run the unpatched sqlglot from disk."""
    global _UNVERIFIABLE
    if _UNVERIFIABLE is None:
        _UNVERIFIABLE = set()
        for p in (SRC / "tests").rglob("*.py"):
            text = p.read_text(errors="replace")
            if "subprocess" not in text and "os.system" not in text and "Popen" not in text:
                continue
            rel = p.relative_to(SRC).as_posix()
            tree = ast.parse(text)
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for fn in cls.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        seg = ast.get_source_segment(text, fn) or ""
                        if "subprocess" in seg or "os.system" in seg or "Popen" in seg:
                            _UNVERIFIABLE.add(f"{rel}::{cls.name}::{fn.name}")
                    if isinstance(fn, ast.FunctionDef) and fn.name.startswith("setUp"):
                        seg = ast.get_source_segment(text, fn) or ""
                        if "subprocess" in seg or "Popen" in seg:
                            _UNVERIFIABLE.add(f"{rel}::{cls.name}::*")
    return _UNVERIFIABLE


def split_cost(workers):
    if not workers:
        return 0
    return max(sum(MS[t] for t in w) for w in workers) + WORKER_MS * len(workers)


def estimate_cpu(sel, vcost):
    est = 0.15 + sum(1.2 * SECONDS[t] for t in sel)
    for cls in {TEST_IDS[t].rsplit("::", 1)[0] for t in sel}:
        est += vcost.get(cls, 0.2)
    return est


def check_runs(plans):
    """plans: commit -> dict(sel=set of map indexes, src=patched sources or None).
    Replaces plan["sel"] with the failing tests for every commit it could check."""
    import time

    try:
        import verify
        vcost = json.loads((HERE / "vcost.json").read_text())
    except Exception as e:
        log(f"no run-time check: {e!r}")
        return
    unver = unverifiable_ids()
    cands = []
    for c, pl in plans.items():
        sel, src = pl["sel"], pl["src"]
        if not sel or src is None or len(sel) == N:
            continue
        ids = [TEST_IDS[t] for t in sorted(sel)]
        if any(i in unver or i.rsplit("::", 1)[0] + "::*" in unver for i in ids):
            continue
        cost = split_cost(split(sel)) / 1000
        est = estimate_cpu(sel, vcost)
        if P_PASS * cost > est:
            cands.append((cost - est, c, ids, est))
    if not cands:
        return
    cands.sort(reverse=True)
    heavy = sum(1 for _, _, ids, _ in cands if any(i.startswith("tests/test_executor.py") for i in ids))
    verify.preload(heavy=heavy >= 2)
    used = 0.0
    root = str(SRC)
    for gain, c, ids, est in cands:
        if used + est > CPU_BUDGET or time.monotonic() - T0 > WALL_BUDGET:
            log(f"{c}: check skipped (budget)")
            continue
        try:
            bad, cpu, info = verify.run(root, plans[c]["src"], ids, timeout=3 + 3 * est)
        except Exception as e:
            log(f"{c}: check error {e!r}")
            continue
        used += cpu
        if bad is None:
            log(f"{c}: check gave no answer ({info}), keeping coverage set")
            continue
        idx = {TEST_IDS[t]: t for t in plans[c]["sel"]}
        plans[c]["sel"] = {idx[i] for i in bad if i in idx}
        log(f"{c}: checked {len(ids)} tests in {cpu:.2f}s cpu (est {est:.2f}): {len(bad)} fail")


def definition_checks(plans):
    """A changed line that runs at import time can change a constant or a class attribute that
    tests read later without running that line. Import the patched sqlglot in a child, compare
    every module constant and class attribute with the pinned ones, and add the users of what
    changed. Any doubt adds the users of the whole module."""
    need = [c for c, pl in plans.items() if pl["defs"]]
    if not need:
        return
    import verify

    verify.preload()
    root = str(SRC)
    base, cpu0 = verify.snapshot(root, {})
    log(f"pinned snapshot: {None if base is None else len(base)} keys, {cpu0:.2f}s cpu")
    for c in need:
        pl = plans[c]
        snap = None
        if base is not None:
            snap, cpu = verify.snapshot(root, pl["py"])
        if snap is None:
            for rel in pl["defs"]:
                pl["sel"] |= module_users(rel)
            log(f"{c}: definition check failed, added module users")
            continue
        changed = {k for k in set(base) | set(snap) if base.get(k) != snap.get(k)}
        add = definition_users(changed)
        pl["sel"] |= add
        pl["src"] = None if changed else pl["src"]
        log(f"{c}: definitions changed: {len(changed)} {sorted(changed)[:5]} -> +{len(add)} tests ({cpu:.2f}s)")


def main():
    global T0
    import time

    T0 = time.monotonic()
    remap, unknown = load_numbering()
    plans = {}
    for patch in sorted((DATA / "commits").glob("*.patch")):
        src = None
        try:
            text = patch.read_text(errors="replace")
            sel = select(text)
            try:
                src = patched_sources(text)
            except Exception:
                src = None
        except Exception as e:  # never miss because of a bug: run everything
            log(f"{patch.stem}: error {e!r}, running everything")
            sel = set(ALL)
        plans[patch.stem] = {"sel": sel, "src": src, "defs": set(INFO.get("defs", ())),
                             "py": dict(INFO.get("py", {}))}
    try:
        definition_checks(plans)
    except Exception as e:
        log(f"definition check failed: {e!r}")
        for pl in plans.values():
            for rel in pl["defs"]:
                pl["sel"] |= module_users(rel)
    if remap is None and not os.environ.get("NO_CHECK"):
        try:
            check_runs(plans)
        except Exception as e:
            log(f"check failed: {e!r}")
    for c, pl in plans.items():
        sel = pl["sel"]
        if remap is not None:
            sel = {remap[t] for t in sel if t in remap}
            if sel or unknown:
                sel |= set(unknown)  # tests we have no map for: never leave them out
        workers = split(sel)
        print(json.dumps({"commit": c, "workers": workers}))
        log(f"{c}: {len(sel)} tests, {len(workers)} workers")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
