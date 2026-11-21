import json, sys, os, collections
sys.path.insert(0, "/home/claude/br/sel")
import verify as V
root = "/home/claude/br/sqlglot"
cpu = json.load(open("/tmp/cpu_tests.json"))
TT = json.load(open("/home/claude/br/kit/sample/tests.json"))["tests"]; ids=[t["id"] for t in TT]; rec={t["id"]: t["seconds"] for t in TT}
by = collections.defaultdict(list)
for i in ids: by[i.rsplit("::", 1)[0]].append(i)
V.preload(heavy=True)
out = {}
for cls, tl in by.items():
    t = min(tl, key=lambda x: rec[x])
    bad, c, _ = V.run(root, {}, [t], timeout=60)
    out[cls] = round(max(0.0, c - 1.15*rec[t]), 3)
    if bad: print("BAD", t, bad)
json.dump(out, open("/tmp/classcost.json", "w"), indent=0)
for k, v in sorted(out.items(), key=lambda kv: -kv[1])[:15]: print(v, k)
print(len(out), sorted(out.values())[len(out)//2])
