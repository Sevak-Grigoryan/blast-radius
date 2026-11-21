"""Build the coverage map shipped with the selector (run once, offline, on the pinned sqlglot).

Inputs: coverage data with one context per test (tools/covplugin.py), files opened per test,
classes instantiated per test, tests.json. Output: map.json.gz with
  tests:      test number -> seconds;  ids: test number -> id
  sets:       deduplicated sorted lists of test numbers (everything below points into it)
  lines:      file -> {line: set}       tests that executed the line at run time
  imp:        file -> [lines]           lines executed only while importing / collecting
  users:      file -> set               tests that executed any line of the file at run time
  scopes:     file -> [[start, end, set, "c"|"f", qualname]]  classes / functions and their users
  opened:     file -> set               non-Python files each test opened
  classusers: "file::Qualname" -> set   tests that instantiated the class or a subclass
  importers:  file -> [files]           modules that import it (static, one level)
"""
import ast
import gzip
import json
import os
import sys
from collections import defaultdict

import coverage

ROOT, COV, OPENED, TESTS, OUT = sys.argv[1:6]
TYPES = sys.argv[6] if len(sys.argv) > 6 else None

tests = json.load(open(TESTS))["tests"]
num = {t["id"]: t["number"] for t in tests}
by_class = defaultdict(set)
by_file = defaultdict(set)
for t in tests:
    by_class["::".join(t["id"].split("::")[:-1])].add(t["number"])
    by_file[t["file"]].add(t["number"])


def ctx_tests(ctx):
    """Tests a coverage context stands for (None: import / collection / between tests)."""
    if ctx in num:
        return {num[ctx]}
    if ctx.startswith("<setup>"):
        tid = ctx[len("<setup>"):]
        if tid in num:
            # class/module-scoped setup (setUpClass, fixtures) serves every test of its class
            return set(by_class.get("::".join(tid.split("::")[:-1]), set())) | {num[tid]}
    return None


sets, set_idx = [], {}


def sid(s):
    k = tuple(sorted(s))
    if k not in set_idx:
        set_idx[k] = len(sets)
        sets.append(list(k))
    return set_idx[k]


data = coverage.CoverageData(COV)
data.read()
lines_out, imp_out, users_out, scopes_out, lambdas_out = {}, {}, {}, {}, {}
for path in sorted(data.measured_files()):
    rel = os.path.relpath(path, ROOT)
    per_line = defaultdict(set)
    imp = set()
    for line, ctxs in data.contexts_by_lineno(path).items():
        runtime = False
        for c in ctxs:
            ts = ctx_tests(c)
            if ts is None:
                imp.add(line)
            else:
                per_line[line] |= ts
                runtime = True
        if runtime:
            imp.discard(line)
    users = set()
    for s in per_line.values():
        users |= s
    lines_out[rel] = {str(l): sid(s) for l, s in per_line.items()}
    imp_out[rel] = sorted(imp)
    users_out[rel] = sid(users)
    spans = []
    try:
        tree = ast.parse(open(path).read())
    except Exception:
        tree = None

    def visit(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                a = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                b = ch.end_lineno
                u = set()
                for l, s in per_line.items():
                    if a <= l <= b:
                        u |= s
                q = prefix + ch.name
                kind = "c" if isinstance(ch, ast.ClassDef) else "f"
                body = ch.body[0].lineno if ch.body else ch.lineno
                spans.append([a, b, sid(u), kind, q, body])
                visit(ch, q + ("." if kind == "c" else ".<locals>."))
            else:
                visit(ch, prefix)

    lam = []
    if tree is not None:
        visit(tree, "")
        for node in ast.walk(tree):
            if isinstance(node, ast.Lambda) and node.end_lineno > node.lineno:
                lam.append([node.lineno + 1, node.end_lineno])
    lambdas_out[rel] = lam
    spans.sort(key=lambda x: (x[0], -x[1]))
    scopes_out[rel] = spans

op = defaultdict(set)
for ctx, files in json.load(open(OPENED)).items():
    ts = ctx_tests(ctx)
    for f in files:
        if f.endswith(".py"):
            continue
        op[f] |= ts if ts is not None else set(range(len(tests)))
opened_out = {f: sid(s) for f, s in op.items()}

classusers = {}
if TYPES:
    cu = defaultdict(set)
    for ctx, keys in json.load(open(TYPES)).items():
        ts = ctx_tests(ctx)
        if ts is None:
            continue
        for k in keys:
            cu[k] |= ts
    classusers = {k: sid(v) for k, v in cu.items()}

importers = defaultdict(set)
for dirpath, _, files in os.walk(os.path.join(ROOT, "sqlglot")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(dirpath, fn)
        rel = os.path.relpath(p, ROOT)
        try:
            tree = ast.parse(open(p).read())
        except Exception:
            continue
        pkg = os.path.dirname(rel).replace(os.sep, ".")
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = pkg.split(".")
                    parts = parts[: len(parts) - node.level + 1]
                    base = ".".join(parts + ([base] if base else []))
                mods = [base] + [base + "." + a.name for a in node.names]
            for m in mods:
                cand = m.replace(".", os.sep)
                for c in (cand + ".py", os.path.join(cand, "__init__.py")):
                    if os.path.exists(os.path.join(ROOT, c)):
                        importers[c].add(rel)

result = {
    "tests": [t["seconds"] for t in tests],
    "ids": [t["id"] for t in tests],
    "files_of_tests": {f: sid(s) for f, s in by_file.items()},
    "sets": sets,
    "lines": lines_out,
    "imp": imp_out,
    "users": users_out,
    "scopes": scopes_out,
    "opened": opened_out,
    "classusers": classusers,
    "lambdas": lambdas_out,
    "importers": {k: sorted(v) for k, v in importers.items()},
}
raw = json.dumps(result, separators=(",", ":")).encode()
with gzip.open(OUT, "wb", compresslevel=9) as f:
    f.write(raw)
print(f"sets {len(sets)}  files {len(lines_out)}  classes {len(classusers)}  raw {len(raw)/1e6:.1f} MB  gz {os.path.getsize(OUT)/1e6:.2f} MB")
