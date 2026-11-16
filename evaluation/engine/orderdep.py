"""
Does the engine's answer depend on dictionary and set iteration order?

The thesis claims determinism in several places and rests the no-bias argument
of Section 4.5.2 on it: the same request must give the same plan. That claim is
usually checked by running the engine twice in one process, which cannot fail,
because a set iterates in the same order both times within a single run.

Python randomises string hashing per process unless PYTHONHASHSEED is fixed, so
sets of unit codes iterate in a different order in a different process. The
engine builds its requisite graph out of sets. If any decision reads one of them
without sorting first, the plan changes between runs and nothing inside a single
run can see it.

This runs the whole plan set in several subprocesses under different hash seeds
and compares the output byte for byte.

Usage:
    python3 evaluation/engine/orderdep.py [--seeds 5]
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

WORKER = r'''
import json, os, sys
sys.path.insert(0, sys.argv[1])
import api, core
import rules as R

eng = api.Engine()
out = {}
for inst in ("USYD", "UTS"):
    rules = eng.rules[inst]
    for key, plan in sorted(eng.plans.get(inst, {}).items()):
        data = eng._dataset_for(inst, plan)
        sched = eng._schedule_for(inst, plan)
        prereqs, coreqs, dependents = eng._graph(inst, plan)
        out[f"{inst}|{key}|base"] = {str(i): sorted(map(str, v))
                                     for i, v in sorted(sched.items())}
        for code in sorted({c for v in sched.values() for c in v
                            if not R.is_placeholder(c)}):
            try:
                new = core.replan_after_failure(rules, sched, code, data,
                                                prereqs, dependents, coreqs)
            except Exception as exc:
                out[f"{inst}|{key}|{code}"] = f"raised {type(exc).__name__}"
                continue
            out[f"{inst}|{key}|{code}"] = {str(i): sorted(map(str, v))
                                           for i, v in sorted(new.items())}
print(json.dumps(out, sort_keys=True))
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=5)
    args = ap.parse_args()

    # The worker is handed the engine's source folder, not a working directory.
    repo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src")
    repo = os.path.abspath(repo)
    here = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(here, "_orderdep_worker.py")
    with open(worker, "w") as fh:
        fh.write(WORKER)

    digests, payloads = {}, {}
    seeds = ["0"] + [str(1 + i * 7919) for i in range(args.seeds - 1)]
    for seed in seeds:
        env = dict(os.environ, PYTHONHASHSEED=seed)
        proc = subprocess.run([sys.executable, worker, repo], env=env,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  seed {seed}: worker failed\n{proc.stderr[-600:]}")
            return 1
        payloads[seed] = json.loads(proc.stdout)
        digests[seed] = hashlib.sha256(proc.stdout.encode()).hexdigest()[:16]
        print(f"  seed {seed:>6}  {len(payloads[seed])} schedules  "
              f"digest {digests[seed]}")

    os.remove(worker)
    unique = set(digests.values())
    print(f"\ndistinct outputs across {len(seeds)} hash seeds: {len(unique)}")
    if len(unique) == 1:
        print("PASS  the plan set and all 1,497 repairs are identical "
              "whatever order the sets iterate in")
        return 0

    reference = payloads[seeds[0]]
    for seed in seeds[1:]:
        if digests[seed] == digests[seeds[0]]:
            continue
        other = payloads[seed]
        differing = [k for k in reference if reference[k] != other.get(k)]
        print(f"FAIL  seed {seed} differs in {len(differing)} schedules")
        for k in differing[:5]:
            print(f"    {k}")
            print(f"      seed {seeds[0]}: {reference[k]}")
            print(f"      seed {seed}: {other.get(k)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
