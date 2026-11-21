"""Lines of the pinned sqlglot that run while its modules are imported (class bodies, decorators,
metaclasses, module-level calls). A change there acts at definition time, before any test runs."""
import json, os, sys, pkgutil, importlib
import coverage
ROOT = os.path.abspath(sys.argv[1])
out = sys.argv[2]
sys.path.insert(0, ROOT)
cov = coverage.Coverage(data_file=None, source=[os.path.join(ROOT, "sqlglot")])
cov.start()
import sqlglot
failed = []
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(m.name)
    except Exception as e:
        failed.append((m.name, repr(e)[:80]))
cov.stop()
data = cov.get_data()
res = {}
for f in data.measured_files():
    rel = os.path.relpath(f, ROOT)
    res[rel] = sorted(data.lines(f) or [])
json.dump(res, open(out, "w"))
print("files", len(res), "lines", sum(map(len, res.values())), "failed", failed, file=sys.stderr)
