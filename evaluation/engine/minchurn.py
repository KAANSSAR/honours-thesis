"""
What is the fewest units a valid repair has to move?

`optimality.py` asks whether a repair finishes earlier than the engine's. This
asks the other question a student actually feels: how much of the plan had to be
rearranged at all. A repair that finishes on time but reshuffles two thirds of
the degree is not the same as one that moves four units, and Section 4.3.3
measures how many the engine moves without being able to say how many were
necessary.

Same approach as the optimality check and the same caveat: the search is
independent of the engine, but unit data is read through the rules objects, so a
dataset error is inherited rather than caught.

Usage:
    python3 evaluation/engine/minchurn.py "INST|plan key|FAILEDCODE"
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import os
import sys

from ortools.sat.python import cp_model

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help="INST|plan key|FAILED CODE")
    ap.add_argument("--span", type=int, default=None,
                    help="last position allowed; defaults to the published span")
    args = ap.parse_args()

    import api
    import core
    import rules as R

    inst, key, failed = args.scenario.split("|")
    engine = api.Engine()
    plan = engine.plans[inst][key]
    data = engine._dataset_for(inst, plan)
    rules = engine.rules[inst]
    prereqs, coreqs, _ = engine._graph(inst, plan)
    schedule = engine._schedule_for(inst, plan)

    original = {}
    for index, codes in schedule.items():
        for code in codes:
            if not R.is_placeholder(code):
                original[R.base_code(str(code))] = index

    span = args.span if args.span is not None else max(
        i for i, v in schedule.items() if v)
    positions = [i for i in range(span + 1) if R.is_real(i)]
    failed_at = original[failed]

    m = cp_model.CpModel()
    at = {}          # (code, position) -> bool
    for code in original:
        for p in positions:
            at[code, p] = m.NewBoolVar(f"{code}@{p}")
        m.AddExactlyOne(at[code, p] for p in positions)

    # Availability, read from the rules rather than reimplemented.
    for code in original:
        for p in positions:
            if not rules.offered_in(code, p, data):
                m.Add(at[code, p] == 0)

    pos = {}
    for code in original:
        v = m.NewIntVar(0, span, f"pos_{code}")
        m.Add(v == sum(p * at[code, p] for p in positions))
        pos[code] = v

    # The retake sits strictly after the period it was failed in.
    m.Add(pos[failed] > failed_at)

    # Prerequisites: at least one member of each group strictly earlier. A group
    # whose members are all outside this plan is left alone, exactly as the
    # engine leaves it: there is nothing in the schedule to order against.
    for code in original:
        unit = data["units"].get(R.base_code(code), {})
        if unit.get("requisite_waived_for_some_courses"):
            continue
        groups = unit.get("prerequisite_groups") or []
        for group in groups:
            members = [g for g in group if g in original]
            if not members:
                continue
            picks = []
            for g in members:
                b = m.NewBoolVar(f"{code}_after_{g}")
                m.Add(pos[g] < pos[code]).OnlyEnforceIf(b)
                picks.append(b)
            m.AddAtLeastOne(picks)

        for group in (unit.get("corequisite_codes") and
                      [unit["corequisite_codes"]] or []):
            members = [g for g in group if g in original]
            if not members:
                continue
            picks = []
            for g in members:
                b = m.NewBoolVar(f"{code}_with_{g}")
                m.Add(pos[g] <= pos[code]).OnlyEnforceIf(b)
                picks.append(b)
            m.AddAtLeastOne(picks)

    # Load. The failed attempt still occupies its period.
    failed_cp = rules.unit_cp(failed, data)
    over = []
    for p in positions:
        load = sum(rules.unit_cp(c, data) * at[c, p] for c in original)
        if p == failed_at:
            load = load + failed_cp
        m.Add(load <= rules.max_cp_per_period)
        b = m.NewBoolVar(f"over_{p}")
        m.Add(load > rules.min_cp_per_period).OnlyEnforceIf(b)
        m.Add(load <= rules.min_cp_per_period).OnlyEnforceIf(b.Not())
        over.append(b)
    m.Add(sum(over) <= 1)          # the engine's budget: one overloaded period

    moved = []
    for code, index in original.items():
        b = m.NewBoolVar(f"moved_{code}")
        m.Add(at[code, index] == 0).OnlyEnforceIf(b)
        m.Add(at[code, index] == 1).OnlyEnforceIf(b.Not())
        moved.append(b)
    m.Minimize(sum(moved))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 120
    solver.parameters.num_search_workers = 8
    status = solver.Solve(m)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        print(f"no repair within the published span: {solver.StatusName(status)}")
        return 1

    result = {c: solver.Value(pos[c]) for c in original}
    changed = sorted(c for c in original if result[c] != original[c])
    proved = status == cp_model.OPTIMAL

    print(f"{inst} {key}")
    print(f"failing {failed}, staying inside {rules.position_label(span)}\n")
    print(f"fewest units that must move: {len(changed)}"
          f"{' (proved)' if proved else ' (best found)'}\n")
    for c in changed:
        print(f"  {c:10} {rules.position_label(original[c]):24} -> "
              f"{rules.position_label(result[c])}")

    rebuilt = {}
    for c, p in result.items():
        rebuilt.setdefault(p, []).append(c)
    rebuilt.setdefault(failed_at, []).append(f"{R.FAILED_PREFIX}{failed}")
    print()
    for p in sorted(rebuilt):
        cp = core.period_credit_points(rules, rebuilt, p, data)
        print(f"  {rules.position_label(p):24} {cp:3}cp  {sorted(rebuilt[p])}")

    violations = core.validate_schedule(rules, rebuilt, prereqs, coreqs, data)
    print()
    print("validator on the rebuilt schedule:",
          "clean" if not violations else violations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
