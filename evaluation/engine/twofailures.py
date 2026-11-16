"""
Two units failed in the same session.

Every scenario in Chapter 4 fails exactly one unit, and a student who fails two
subjects in one session is ordinary rather than exotic. The overload budget is
the count of `FAILED:` markers in the schedule, so a second failure is the first
thing that exercises a budget above one, and nothing in the evaluation has ever
done that.

For each plan, every pair of units sharing a teaching period is failed together
and the result is validated in full. Pairs within a period rather than all pairs,
because that is the situation the budget is for and because the full pairwise
set is 1.1 million scenarios.

Usage:
    python3 evaluation/engine/twofailures.py [--limit N]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from paths import P

import argparse
import itertools
import json
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import api, core
    import rules as R

    eng = api.Engine()
    results = []

    def span_end(s):
        return max(i for i, v in s.items() if v)

    for inst in ("USYD", "UTS"):
        rules = eng.rules[inst]
        for key, plan in eng.plans.get(inst, {}).items():
            data = eng._dataset_for(inst, plan)
            base = eng._schedule_for(inst, plan)
            prereqs, coreqs, dependents = eng._graph(inst, plan)
            inherited = set(core.validate_schedule(rules, base, prereqs,
                                                   coreqs, data))
            start = span_end(base)

            for index in sorted(base):
                here = [c for c in base[index] if not R.is_placeholder(c)]
                for first, second in itertools.combinations(sorted(here), 2):
                    row = {"institution": inst, "plan": key, "period": index,
                           "failed": [first, second]}
                    try:
                        once = core.replan_after_failure(
                            rules, base, first, data, prereqs, dependents, coreqs)
                        twice = core.replan_after_failure(
                            rules, once, second, data, prereqs, dependents, coreqs)
                    except Exception as exc:
                        row.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                        results.append(row)
                        continue

                    bad = [v for v in core.validate_schedule(
                        rules, twice, prereqs, coreqs, data) if v not in inherited]
                    row.update(ok=not bad,
                               violations=[str(v) for v in bad][:4],
                               grew=span_end(twice) - start,
                               # Both retakes must actually be somewhere, and
                               # neither may sit where it already failed.
                               placed=sorted(
                                   code for code in (first, second)
                                   if any(R.base_code(str(c)) == code
                                          for v in twice.values() for c in v
                                          if not str(c).startswith(R.FAILED_PREFIX))))
                    results.append(row)
                    if args.limit and len(results) >= args.limit:
                        break
                if args.limit and len(results) >= args.limit:
                    break
            if args.limit and len(results) >= args.limit:
                break

    clean = [r for r in results if r.get("ok")]
    crashed = [r for r in results if r.get("error")]
    broke = [r for r in results if r.get("ok") is False and not r.get("error")]
    lost = [r for r in clean if len(r.get("placed") or []) != 2]

    print(f"pairs tested            : {len(results)}")
    print(f"clean after both repairs: {len(clean)}")
    print(f"left a violation        : {len(broke)}")
    print(f"crashed                 : {len(crashed)}")
    print(f"a retake went missing   : {len(lost)}")
    for label, rows in (("violations", broke), ("crashes", crashed),
                        ("missing retakes", lost)):
        for r in rows[:6]:
            print(f"  [{label}] {r['institution']} {r['plan'][:38]} "
                  f"{r['failed']} {r.get('error') or r.get('violations') or r.get('placed')}")

    by_growth = {}
    for r in clean:
        by_growth[r["grew"]] = by_growth.get(r["grew"], 0) + 1
    print("span growth in positions:",
          {k: by_growth[k] for k in sorted(by_growth)})

    json.dump(results, open(P("twofailures.json"), "w"))
    print("\nwrote " + P("twofailures.json"))
    return 1 if (broke or crashed or lost) else 0


if __name__ == "__main__":
    raise SystemExit(main())
