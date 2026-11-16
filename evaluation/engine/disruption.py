"""Re-measure Table 4.6 (tab:minimal): disruption caused by a single failure.

Growth is counted in teaching periods, not raw position indices: positions 1
and 3 of each year are the winter and summer intensive windows, so a plan that
ends in Year 4 Semester 2 (index 14) and grows to Year 5 Semester 1 (index 16)
has grown by one semester, not two positions.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import os, statistics, sys

import api, core
import rules as R

SEM = (0, 2)   # semester slots within a year


def span_end(sched):
    return max(i for i, v in sched.items() if v)


def semesters_between(old, new):
    """Teaching semesters strictly after `old` and at or before `new`."""
    return sum(1 for i in range(old + 1, new + 1) if i % R.POSITIONS_PER_YEAR in SEM)


def moved(before, after):
    def pos(s):
        out = {}
        for i, codes in s.items():
            for c in codes:
                c = str(c)
                if c.startswith(R.ELECTIVE_PREFIX):
                    continue
                out[R.base_code(c)] = i
        return out
    b, a = pos(before), pos(after)
    return sum(1 for k in b if k in a and a[k] != b[k])


def run():
    eng = api.Engine()
    out = {}
    for inst in ("USYD", "UTS"):
        rows = []
        rules = eng.rules[inst]
        for key, plan in eng.plans.get(inst, {}).items():
            data = eng._dataset_for(inst, plan)
            sched = eng._schedule_for(inst, plan)
            prereqs, coreqs, dependents = eng._graph(inst, plan)
            inherited = set(core.validate_schedule(rules, sched, prereqs, coreqs, data))
            base_end = span_end(sched)
            for code in sorted({c for v in sched.values() for c in v
                                if not R.is_placeholder(c)}):
                try:
                    new = core.replan_after_failure(rules, sched, code, data,
                                                    prereqs, dependents, coreqs)
                except Exception:
                    rows.append(None)
                    continue
                bad = [v for v in core.validate_schedule(rules, new, prereqs,
                                                          coreqs, data)
                       if v not in inherited]
                rows.append((not bad,
                             semesters_between(base_end, span_end(new)),
                             moved(sched, new)))
        out[inst] = rows
    return out


if __name__ == "__main__":
    if "--no-compact" in sys.argv:
        # Reproduces the engine as it stood before compaction was ported, so
        # both columns of the table come from the same code.
        core.compact_schedule = lambda rules, s, *a, **k: s
        core.rebalance_schedule = lambda rules, s, *a, **k: s
    for inst, rows in run().items():
        ok = [r for r in rows if r]
        clean = sum(1 for r in ok if r[0])
        inspan = sum(1 for r in ok if r[1] == 0)
        one = sum(1 for r in ok if r[1] == 1)
        year = sum(1 for r in ok if r[1] >= 2)
        med = statistics.median([r[2] for r in ok])
        print(f"{inst:5} scenarios {len(rows):5}  clean {clean:5}  "
              f"in span {inspan:5}  +1 sem {one:5}  +>=1 year {year:5}  "
              f"median moved {med}")
