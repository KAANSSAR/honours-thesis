"""
Mutation testing for the planning engine.

A check that passes a deliberately broken engine is worth nothing. This injects
one fault at a time into the engine, re-runs the independent audit, and records
whether the audit notices. A mutant the audit cannot distinguish from the real
engine is a gap in the checking, not a gap in the engine.

The mutation score is killed / total. It is reported per mutant as well, because
which faults survive says more than the ratio: a surviving mutant names a rule
nothing is actually testing.

Each mutant runs in its own copy of the repository, so a failed patch cannot
contaminate the next one or the working tree.

Usage:
    python3 evaluation/engine/mutation.py [--sample N]
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

# Each mutation is a plain textual substitution with a stated intent. They are
# deliberately the kind of mistake a person makes: an off-by-one in a comparison,
# a dropped guard, a step skipped.
MUTANTS = [
    ("prereq accepts same period",
     "core.py",
     "any(p in semester_of and semester_of[p] < target for p in group)",
     "any(p in semester_of and semester_of[p] <= target for p in group)",
     "rules_usyd.py"),

    ("load ceiling off by one",
     "core.py",
     "+ rules.unit_cp(code, data) > cap)",
     "+ rules.unit_cp(code, data) > cap + 6)",
     None),

    ("retake may precede its own failure",
     "core.py",
     "if failed_at and target <= max(failed_at):\n        return False",
     "if False:\n        return False",
     None),

    ("repair loop never runs",
     "core.py",
     "for _ in range(R.MAX_SEARCH_HORIZON):\n        broken = []",
     "for _ in range(0):\n        broken = []",
     None),

    ("compaction disabled",
     "core.py",
     "compacted = compact_schedule(rules, new_schedule, data, prereqs, coreqs)",
     "compacted = dict(new_schedule)",
     None),

    ("search may overload freely",
     "core.py",
     "    if cap is None:\n        cap = rules.min_cp_per_period",
     "    if cap is None:\n        cap = rules.max_cp_per_period * 10",
     None),

    ("corequisite check removed",
     "core.py",
     "and rules.coreq_satisfied(code, target, semester_of, coreqs, data)",
     "and True",
     None),

    ("availability ignored when placing",
     "core.py",
     "and rules.offered_in(code, target, data))",
     "and True)",
     None),

    ("cascade skips dependents entirely",
     "core.py",
     "    for code in affected:\n        if still_satisfied(code, semester_of[code]):",
     "    for code in []:\n        if still_satisfied(code, semester_of[code]):",
     None),
]


def run_audit(repo, sample):
    """Return (plan_findings, repairs_with_new_violations) or None if it crashed."""
    # Run the copy's own checker, so it imports the copy's (mutated) engine.
    audit = os.path.join(repo, "evaluation", "engine", "audit.py")
    cmd = [sys.executable, "-I", audit, "--replan"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return "timeout"
    if out.returncode != 0:
        return "crash"
    plans = repairs = None
    for line in out.stdout.splitlines():
        if line.startswith("plans with findings"):
            plans = int(line.split(":")[1].split("/")[0])
        if line.startswith("repairs that introduce a violation"):
            repairs = int(line.split(":")[1])
    if plans is None or repairs is None:
        return "unparsed"
    return (plans, repairs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    args = ap.parse_args()
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    print("baseline (the real engine):")
    base = run_audit(repo, args.sample)
    print(f"   {base}")
    if not isinstance(base, tuple):
        print("baseline did not produce numbers; stopping")
        return

    killed = survived = broke = 0
    rows = []
    for name, target_file, old, new, alt_file in MUTANTS:
        work = tempfile.mkdtemp(prefix="mutant_")
        dest = os.path.join(work, "repo")
        shutil.copytree(repo, dest, ignore=shutil.ignore_patterns(
            "__pycache__", "venv", ".venv", ".git", "uts_cache", "*.pyc",
            "docs", "results", "archive"))
        # the cache is needed by nothing at audit time, but the data files are
        applied = False
        for candidate in (target_file, alt_file):
            if not candidate:
                continue
            path = os.path.join(dest, "src", candidate)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            if old in text:
                open(path, "w", encoding="utf-8").write(text.replace(old, new, 1))
                applied = True
                break
        if not applied:
            rows.append((name, "NOT APPLIED", "pattern not found"))
            shutil.rmtree(work, ignore_errors=True)
            continue

        result = run_audit(dest, args.sample)
        shutil.rmtree(work, ignore_errors=True)

        if not isinstance(result, tuple):
            broke += 1
            rows.append((name, "crashed", str(result)))
        elif result != base:
            killed += 1
            rows.append((name, "KILLED",
                         f"plans {base[0]}->{result[0]}, repairs {base[1]}->{result[1]}"))
        else:
            survived += 1
            rows.append((name, "SURVIVED", "audit saw no difference"))

    print(f"\n{'mutant':44s} {'verdict':10s} detail")
    print("-" * 100)
    for name, verdict, detail in rows:
        print(f"{name:44s} {verdict:10s} {detail}")

    total = killed + survived
    print(f"\nkilled {killed}, survived {survived}, crashed {broke}")
    if total:
        print(f"mutation score: {killed}/{total} = {100*killed/total:.0f}%")
    if survived:
        print("\nA surviving mutant means the audit cannot tell that engine apart\n"
              "from the real one. That names a rule nothing is really testing.")


if __name__ == "__main__":
    main()
