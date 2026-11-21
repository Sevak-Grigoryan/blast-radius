"""Merge the per-worker files of tools/typeprof.py into typemap.json.gz.

    python tools/build_typemap.py /tmp/brcov/typeprof.json* typemap.json.gz
"""
import gzip
import json
import sys

types, sites = {}, {}
for fn in sys.argv[1:-1]:
    d = json.load(open(fn))
    types.update(d["types"])
    for k, pairs in d["sites"].items():
        s = sites.setdefault(k, set())
        s.update(tuple(p) for p in pairs)

if "builtins.NoneType" in types:  # files written before typeprof knew None compares by identity
    types["builtins.NoneType"]["id"] = True
names = sorted(types)
idx = {n: i for i, n in enumerate(names)}
out = {
    # [name, compares by identity, enum, bool, class defining __eq__, class defining __ne__]
    "types": [[n, int(types[n]["id"]), int(types[n]["enum"]), int(types[n]["bool"]),
               types[n].get("eq", "?"), types[n].get("ne", "?")] for n in names],
    "sites": {k: sorted([idx[a], idx[b]] for a, b in v) for k, v in sorted(sites.items())},
}
with gzip.open(sys.argv[-1], "wt") as f:
    json.dump(out, f, separators=(",", ":"))
print(f"{len(sites)} sites, {len(names)} types")
