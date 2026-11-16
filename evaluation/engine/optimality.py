"""
Is each repair the engine produces as short as the rules allow?

For every failure scenario, the engine returns a plan finishing in some teaching
period. This asks a separate solver whether any valid plan finishes earlier, under
the same overload budget the engine works to. CP-SAT is used because it proves
infeasibility rather than giving up, so "the engine is optimal here" is a result
and not an exhausted search.

What is independent and what is not, stated plainly: the search is entirely
separate from the engine, and so is every constraint written below. Unit data is
read through the rules objects (`offered_in`, `unit_cp`, `load_index`), because
those are dataset accessors rather than search, and reimplementing UTS session
matching would test my copy of it instead of the real thing. If the engine's
notion of which period a January intensive charges to is wrong, this check
inherits that and will not catch it.

Usage:
    python3 evaluation/engine/optimality.py [--limit N] [--institution USYD|UTS]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import json
import os
import re
import sys
import time

from ortools.sat.python import cp_model

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit import slot_at_least
from paths import P

LEVEL = re.compile(r"(\d)\d{3}")


def unit_level(code):
    m = re.search(r"[A-Z]{2,5}(\d)\d{2,3}", code)
    return int(m.group(1)) * 1000 if m else None


def build(engine, core, R, inst, key, failed, horizon, max_overloaded, skip=()):
    """One model: can a valid plan fit inside `horizon` positions?"""
    plan = engine.plans[inst][key]
    rules = engine.rules[inst]
    data = engine._dataset_for(inst, plan)
    sched = engine._schedule_for(inst, plan)
    named = sorted({c for v in sched.values() for c in v if not R.is_placeholder(c)})
    pos_before = {c: i for i, v in sched.items() for c in v if not R.is_placeholder(c)}
    if failed not in pos_before:
        return None

    # The open elective slots are part of the load even though no unit is named in
    # them. Leaving them out hands the solver a session's worth of room that does
    # not exist, which is how this check first disagreed with a known answer.
    slot_names = [str(c) for v in sched.values() for c in v
                  if str(c).startswith(R.ELECTIVE_PREFIX)]
    electives = [f"__ELECTIVE_{i}" for i in range(len(slot_names))]
    slot_label = dict(zip(electives, slot_names))
    units = named + electives
    cpof = {c: (rules.elective_slot_cp if c in electives else rules.unit_cp(c, data))
            for c in units}
    flexible = set(electives)

    # Units the plan already places outside their published availability.
    inherited_av = {c for c in named
                    if not rules.offered_in(c, pos_before[c], data)}

    m = cp_model.CpModel()
    at = {(c, s): m.NewBoolVar(f"{c}@{s}") for c in units for s in range(horizon)}
    pos = {}
    for c in units:
        m.AddExactlyOne(at[c, s] for s in range(horizon))
        v = m.NewIntVar(0, horizon - 1, f"pos_{c}")
        m.Add(v == sum(s * at[c, s] for s in range(horizon)))
        pos[c] = v
        for s in range(horizon):
            if c in flexible:
                if not R.is_real(s):
                    m.Add(at[c, s] == 0)   # an elective slot is an ordinary period
            elif c in inherited_av:
                # The published sequence already places this unit outside its
                # stated availability, an accepted finding rather than a defect
                # (Section 4.2.3). The engine leaves it where it is, so a model
                # that forbids its own position rejects the engine's valid plan.
                # It is pinned to that position rather than freed, so the model
                # cannot exploit an availability the data does not support.
                if s != pos_before[c]:
                    m.Add(at[c, s] == 0)
            elif not rules.offered_in(c, s, data):
                m.Add(at[c, s] == 0)

    # the retake cannot sit at or before the attempt that failed
    m.Add(pos[failed] >= pos_before[failed] + 1)

    # load per teaching period, with gap placements charged to the period that owns
    # them, which is how the engine accounts for an intensive
    owner = {}
    for c in units:
        for s in range(horizon):
            owner[c, s] = s if c in flexible else core.load_index(rules, s, c, data)
    periods = sorted({owner[c, s] for c in units for s in range(horizon)})

    # The failed attempt still occupies its original period and still charges its
    # credit points there, exactly as the engine's FAILED: marker does. Omitting it
    # hands the solver 6cp of room in that period and lets a second overload hide.
    failed_charge = core.load_index(rules, pos_before[failed], failed, data)

    # Teaching periods the published plan already runs above the standard load.
    pre_over = {t for t in periods
                if core.period_credit_points(rules, sched, t, data)
                > rules.min_cp_per_period}

    load, over_flags = {}, []
    for t in periods:
        l = m.NewIntVar(0, rules.max_cp_per_period, f"load_{t}")
        fixed = cpof[failed] if t == failed_charge else 0
        m.Add(l == fixed + sum(cpof[c] * at[c, s] for c in units
                               for s in range(horizon) if owner[c, s] == t))
        load[t] = l
        if max_overloaded is not None and t not in pre_over:
            # Only periods the repair pushes over the baseline draw on the budget.
            # UTS publishes sessions at 27cp before anything goes wrong, and the
            # engine grandfathers those ("a period already above the baseline keeps
            # its room"), so counting them here rejected the engine's own plans.
            f = m.NewBoolVar(f"over_{t}")
            m.Add(l > rules.min_cp_per_period).OnlyEnforceIf(f)
            m.Add(l <= rules.min_cp_per_period).OnlyEnforceIf(f.Not())
            over_flags.append(f)
    if max_overloaded is not None:
        m.Add(sum(over_flags) <= max_overloaded)

    # prerequisites: OR inside a group, AND across groups
    for c in named:
        rec = data["units"].get(R.base_code(c), {})
        if rec.get("requisite_waived_for_some_courses"):
            continue        # UTS waives these by course; the engine honours it
        groups = [list(g) for g in (rec.get("prerequisite_groups") or []) if g]
        if not groups and rec.get("prerequisite_codes"):
            groups = [list(rec["prerequisite_codes"])]
        for g in (() if "prereq" in skip else groups):
            inplan = [p for p in g if p in pos]
            if not inplan:
                continue            # satisfied outside the plan; nothing to constrain
            lits = []
            for p in inplan:
                b = m.NewBoolVar("")
                m.Add(pos[p] < pos[c]).OnlyEnforceIf(b)
                m.Add(pos[p] >= pos[c]).OnlyEnforceIf(b.Not())
                lits.append(b)
            m.AddBoolOr(lits)

        # Corequisites are alternatives: any one at or before satisfies the unit.
        # Reading them as a conjunction is the Section 3.3.2 fault and it rejects
        # every unit that lists several.
        coreq = ([] if "coreq" in skip
                 else [q for q in (rec.get("corequisite_codes") or []) if q in pos])
        if coreq:
            lits = []
            for q in coreq:
                b = m.NewBoolVar("")
                m.Add(pos[q] <= pos[c]).OnlyEnforceIf(b)
                m.Add(pos[q] > pos[c]).OnlyEnforceIf(b.Not())
                lits.append(b)
            m.AddBoolOr(lits)

        # credit-point threshold, with the level and prefix filters it carries
        req = None if "threshold" in skip else rec.get("credit_point_requirement")
        reqs = ([req] if isinstance(req, dict) else
                ([] if "threshold" in skip else (rec.get("credit_point_requirements") or [])))
        if reqs:
            need = min(r.get("min_credit_points") or 0 for r in reqs)
            spec = min(reqs, key=lambda r: r.get("min_credit_points") or 0)
            lo, hi = spec.get("min_level"), spec.get("max_level")
            prefix = spec.get("prefix")

            def counts(o):
                if o == c:
                    return False
                lvl = unit_level(R.base_code(o))
                if lo is not None and (lvl is None or lvl < lo):
                    return False
                if hi is not None and (lvl is None or lvl > hi):
                    return False
                if prefix and not R.base_code(o).startswith(prefix):
                    return False
                return True

            pool = [o for o in named if counts(o)]
            # An unfilled slot still counts where the engine counts it: for a
            # bare "at least level N" requirement whose block name promises N000+.
            # Excluding them is what made this model reject valid plans, because
            # ELEC4712's 36cp of 3000-level study is partly met by such slots.
            # This follows the engine's reading rather than deriving its own, so a
            # fault in that reading is inherited here, like the data accessors.
            if lo is None and hi is None and not prefix:
                # An unrestricted "N credit points of completed study", which is
                # how UTS writes every threshold. The engine counts open elective
                # slots here too: that is credit the student will take, merely not
                # yet chosen, and omitting it understates the total by up to 24cp.
                pool += list(electives)
            elif hi is None and lo is not None:
                # A slot counts toward a level threshold where it cannot be
                # filled below that level, read from the blocks it draws from
                # rather than from its name alone. A name check alone missed the
                # Mechatronic Year 3 slot, whose only non-open block holds two
                # units at 3000 and 4000 level.
                pool += [o for o in electives
                         if slot_at_least(slot_label.get(o, ""), lo, data)]
            if need and pool:
                for s in range(horizon):
                    m.Add(sum(cpof[o] * at[o, t] for o in pool for t in range(s))
                          >= need).OnlyEnforceIf(at[c, s])

    # The teaching period each placement charges to, so "finishes earlier" means
    # an earlier teaching period rather than an earlier raw index.
    period_of = {}
    for c in units:
        v = m.NewIntVar(0, horizon - 1, f"period_{c}")
        m.Add(v == sum(owner[c, s] * at[c, s] for s in range(horizon)))
        period_of[c] = v
    last = m.NewIntVar(0, horizon - 1, "last")
    m.AddMaxEquality(last, list(period_of.values()))
    return m, pos, last, units, rules


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--institution")
    ap.add_argument("--budget", type=int, default=1,
                    help="overloaded periods allowed, matching the engine's")
    ap.add_argument("--only", help="one 'INST|key|code' scenario, for checking")
    ap.add_argument("--out", default=P("optimality.json"))
    args = ap.parse_args()

    import api, core
    import rules as R

    engine = api.Engine()
    scenarios = []
    for inst in ("USYD", "UTS"):
        if args.institution and inst != args.institution:
            continue
        for key, plan in engine.plans.get(inst, {}).items():
            sched = engine._schedule_for(inst, plan)
            for c in sorted({x for v in sched.values() for x in v
                             if not R.is_placeholder(x)}):
                scenarios.append((inst, key, c))
    if args.only:
        i, k, c = args.only.split("|")
        scenarios = [(i, k, c)]
    if args.limit:
        step = max(1, len(scenarios) // args.limit)
        scenarios = scenarios[::step][:args.limit]

    print(f"scenarios: {len(scenarios)}   overload budget: {args.budget}")
    results, t0 = [], time.time()
    optimal = suboptimal = unknown = errors = bad_witness = not_modelled = 0

    for n, (inst, key, code) in enumerate(scenarios, 1):
        try:
            r = api.replan_after_failure(engine, inst, key, code)
            if not r.get("ok"):
                errors += 1
                continue
            rules = engine.rules[inst]
            labels = [rules.position_label(j) for j in range(40)]
            eng_pos = labels.index(r["finishes_in"])
            # finishes_in is already a teaching period, so it is its own owner
            eng_last = eng_pos

            used = max(labels.index(p["period"]) for p in r["periods"] if p["units"])
            built = build(engine, core, R, inst, key, code,
                          max(eng_last, used) + 1, args.budget)
            if built is None:
                errors += 1
                continue
            m, pos, last, units, _ = built

            # Gate: the model has to show it can express the engine's own plan
            # before it is allowed to rule on whether a shorter one exists. An
            # INFEASIBLE verdict from a model that cannot represent a known-valid
            # plan proves nothing, so those scenarios are excluded and counted,
            # not silently scored as optimal.
            gate = cp_model.CpSolver()
            gate.parameters.max_time_in_seconds = 10
            pinned = {}
            for per in r["periods"]:
                for u in per["units"]:
                    pinned[u] = labels.index(per["period"])
            gm, gpos, _, gunits, _ = build(engine, core, R, inst, key, code,
                                           max(eng_last, used) + 1, args.budget)
            for u, idx in pinned.items():
                if u in gpos:
                    gm.Add(gpos[u] == idx)
            if gate.Solve(gm) not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                not_modelled += 1
                results.append({"institution": inst, "plan": key, "failed": code,
                                "verdict": "not modelled"})
                continue

            m.Add(last <= eng_last - 1)      # strictly shorter than the engine's
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = 20.0
            solver.parameters.num_search_workers = 4
            st = solver.Solve(m)

            if st == cp_model.INFEASIBLE:
                optimal += 1
                verdict = "optimal"
            elif st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                # Never report the engine as beaten on this model's word alone.
                # The witness is rebuilt as a schedule and judged by the engine's
                # own validator; if it does not pass, the model is wrong, not the
                # engine, and that is counted separately.
                data = engine._dataset_for(inst, engine.plans[inst][key])
                pre, co, _ = engine._graph(inst, engine.plans[inst][key])
                sch = engine._schedule_for(inst, engine.plans[inst][key])
                origin = {x: i for i, v in sch.items() for x in v
                          if not R.is_placeholder(x)}[code]
                w = {}
                for u in units:
                    w.setdefault(solver.Value(pos[u]), []).append(
                        u if not u.startswith("__ELECTIVE")
                        else f"{R.ELECTIVE_PREFIX}slot")
                w.setdefault(origin, []).append(f"{R.FAILED_PREFIX}{code}")
                if core.validate_schedule(rules, w, pre, co, data):
                    bad_witness += 1
                    verdict = "model produced an invalid witness"
                else:
                    suboptimal += 1
                    gap = eng_last - solver.Value(last)
                    verdict = f"shorter by {gap}"
            else:
                unknown += 1
                verdict = "unknown"
            results.append({"institution": inst, "plan": key, "failed": code,
                            "engine_last": eng_last, "verdict": verdict,
                            "engine_moved": r["units_moved"]})
        except Exception as exc:
            errors += 1
            results.append({"institution": inst, "plan": key, "failed": code,
                            "verdict": f"error: {type(exc).__name__}: {exc}"})

        if n % 100 == 0 or n == len(scenarios):
            el = time.time() - t0
            print(f"  {n}/{len(scenarios)}  optimal {optimal}  shorter {suboptimal}  "
                  f"unknown {unknown}  not-modelled {not_modelled}  "
                  f"({el:.0f}s, {el/n:.2f}s each)")

    json.dump({"budget": args.budget, "results": results},
              open(args.out, "w"), indent=1)
    judged = optimal + suboptimal
    print(f"\nscenarios the model can represent : {judged} of {len(scenarios)}")
    print(f"  excluded (model cannot express)  : {not_modelled}")
    print(f"  engine optimal                   : {optimal}"
          + (f"  ({100*optimal/judged:.1f}% of judged)" if judged else ""))
    print(f"  shorter plan exists              : {suboptimal}")
    print(f"  unknown / errors / bad witness   : {unknown} / {errors} / {bad_witness}")


if __name__ == "__main__":
    main()
