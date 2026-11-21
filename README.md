# Blast Radius selector (RWX track)

For each patch to sqlglot at `eb11aec`, this selects the tests the patch can break and splits them over workers. It works from maps built once from the pinned commit: coverage, imports, the fixture files each test opened, and the lines that run at import time. No patch is looked up in a table. When a candidate set is cheap, the selector also runs those tests against the patched code, which the rules allow (that CPU time is charged).

## How it decides

1. **Python files.** The selector rebuilds the patched file from the pinned sources in `src.tar.gz`, a `git archive` of `sqlglot/` and `tests/`. It diffs the token streams of the old and new versions, ignoring comments and blank lines, so a comment-only change selects nothing. Every changed token becomes a line of the pinned file.
2. **Changed line → tests**, from `map.json.gz`:
   - **Run-time code:** the tests that executed the line, or an earlier line of the same statement. This covers short-circuited conditions. Per-class caches, such as the generator's dispatch table, are emptied before every test during mapping. That way, every test that relies on a cached builder is credited with its lines, not only the first test.
   - **Class bodies:** these are attributes read at run time, such as `WEEK_OFFSET` or `FUNCTIONS`. The selector takes the tests that instantiated or looked up the class (or a subclass), plus the tests that ran its methods.
   - **Function headers** (decorators, signatures, defaults): the tests that ran the function.
   - **Module-level lines:** the tests that used the module, its classes, and the modules that import it.
   - **A file that no longer compiles:** every test that can import it.
3. **Import-time function bodies** (metaclasses, `__init_subclass__`, builders called at module level, constants parsed at import). Lines from `implines.json.gz` run once, while sqlglot is imported, so coverage credits them only to the first test that imported the module. The selector imports the patched sqlglot in a forked child and digests every module constant and class attribute. It compares the result with the pinned digest, then adds the users of each module or class whose values changed. If nothing changed, nothing is added. If the check fails, the users of the whole module are added.
4. **`is` / `==` swaps** (`typemap.json.gz`). When the only code change in a file swaps `is` with `==` (or `is not` with `!=`), the selector looks up each swapped comparison in a type map. The map records the operand types that reached that comparison while the whole pinned suite ran. The swap is a no-op, and selects nothing, when every observed pair gives the same answer both ways:
   - both types compare by identity (no `__eq__`/`__ne__` of their own, or `None`);
   - both are members of the same enum, or both are bools;
   - the types differ and each compares by identity or with sqlglot's `Expression.__eq__`, which is `self is other` across types.

   A comparison the suite never reached, or a patch that edits `expressions/core.py`, gets no such shortcut.
5. **Fixtures and other data files:** the tests that opened them, including class-level `setUpClass` loads.
6. **Run-time check** (`verify.py`). The check runs only when its estimated CPU is below half the cost of running the candidate set on workers. The selector forks a child, serves the patched modules from memory through an import hook, and runs the candidate tests with `unittest`, as pytest does for these `TestCase` classes. Process pools run in-process, so they test the patched code. Then:
   - If every test passes cleanly, the commit gets no workers.
   - Otherwise, only the tests that failed, errored, were skipped, or never reported are kept.
   - On a timeout, a crash, or a test that starts another interpreter, the coverage set is kept.
   - The CPU and wall budgets are capped for the whole batch.
7. **Split:** longest test first onto the least-loaded worker, for 1 to 8 workers. The cheapest split wins (busiest worker plus 2 s per worker).

## The maps

- `tools/covplugin.py` is a pytest plugin, run once on the pinned sqlglot:
  - It runs coverage with one context per test, plus separate contexts for class/module setup.
  - It records the files each test opened and the classes each test instantiated or looked up.
  - It runs process-pool work in-process and empties per-class caches before each test.
- `tools/build_map.py` turns that data into `map.json.gz`.
- `tools/implines.py` records which lines run while every sqlglot module is imported.
- `tools/typeprof.py` is a pytest plugin that rewrites every single-operator `is`/`is not`/`==`/`!=` in sqlglot at import time. It records the operand types at each one. `tools/build_typemap.py` merges the per-worker files into `typemap.json.gz`.
- `tools/cpuplug.py` and `tools/classcost.py` measure per-class setup cost for `vcost.json`, which the run-time check uses to estimate its CPU.

To rebuild the maps:

```sh
git clone https://github.com/tobymao/sqlglot && cd sqlglot && git checkout eb11aec4be8cb2122afe713a4816662c3f0fb2fa
pip install -r ../requirements.lock coverage && pip install -e . --no-deps
SKIP_INTEGRATION=0 SQLGLOT_ROOT=$PWD PYTHONPATH=../tools python -m pytest tests -p covplugin -p no:cacheprovider
python ../tools/build_map.py . /tmp/brcov/.coverage /tmp/brcov/opened.json tests.json map.json.gz /tmp/brcov/types.json
python ../tools/implines.py . implines.json   # then gzip
SKIP_INTEGRATION=0 SQLGLOT_ROOT=$PWD PYTHONPATH=../tools TYPEPROF_FILE=/tmp/brcov/typeprof.json python -m pytest tests -p typeprof -p no:cacheprovider -n 4
python ../tools/build_typemap.py /tmp/brcov/typeprof.json.gw* typemap.json.gz
```

## Sample result

On the 30 sample commits: **114.5 s simulated, 0 misses**, before CPU. The `is`/`==` swaps of 2haz and 66ks no longer pull in `test_execute_tpcds`. Earlier versions scored 176.0 s, then 167.3 s; the starter scored 1614.1 s.
