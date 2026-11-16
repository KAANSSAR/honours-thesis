"""
Run the solver over every default plan at both institutions and report coverage.

    python3 run_all_plans.py                 validate all 51 plans
    python3 run_all_plans.py --replan        also replan after each failure

Reads usyd_default_plans.json, uts_default_plans.json and the two institutions'
datasets. Writes plan_run_results.json. No network access.

This is the Chapter 4 coverage run. One solver, two rule sets, 51 plans: the same
search code validates a Sydney degree and a UTS course, and any difference in the
result comes from the rules rather than from two implementations. That is what
makes the Path A comparison mean what it claims.

The institutions stay in separate stores throughout. Each plan is validated against
its own institution's dataset only, and a requisite that cannot be resolved inside
that dataset is a violation rather than a lookup in the other one.
"""
import json, os, re, sys
from collections import Counter

import core
import rules as R
import rules_usyd
import rules_uts
from paths import P

USYD_PLANS = P("usyd_default_plans.json")
UTS_PLANS = P("uts_default_plans.json")
OUT = P("plan_run_results.json")

# Plan file degree name -> datasets.py degree name.
USYD_DEGREE_ALIAS = {"Software": "Software Engineering"}


def usyd_position(placement):
    """
    Position index for a USYD placement. Sessions are 1 and 2; an intensive has no
    session and names itself instead.

    An Intensive January in "year N" sits in the gap BEFORE that year's Semester 1,
    which is the previous year's summer gap, and its load is charged forward to
    Year N Semester 1. Getting this wrong by one year silently moves a unit past
    its own prerequisites.
    """
    year = placement["year"]
    session = placement.get("session")
    if session == 1:
        return (year - 1) * R.POSITIONS_PER_YEAR + R.POS_SEM1
    if session == 2:
        return (year - 1) * R.POSITIONS_PER_YEAR + R.POS_SEM2

    label = (placement.get("intensive") or "").lower()
    if "jan" in label or "feb" in label:
        return (year - 2) * R.POSITIONS_PER_YEAR + R.POS_SUMMER_GAP
    if "jun" in label or "jul" in label or "aug" in label:
        return (year - 1) * R.POSITIONS_PER_YEAR + R.POS_WINTER_GAP
    return (year - 1) * R.POSITIONS_PER_YEAR + R.POS_SUMMER_GAP


def usyd_schedule(plan):
    """
    The two sides record an unfilled slot differently, so each needs its own
    adapter. USYD keeps it in `placements` with a null code and the blocks it may
    be drawn from in `choose_from`; `open_slots` on a USYD plan is a count, not a
    list. UTS keeps unfilled slots in a separate `open_slots` list.
    """
    schedule = {}
    for p in plan["placements"]:
        index = usyd_position(p)
        code = p.get("code")
        if code:
            schedule.setdefault(index, []).append(code)
        else:
            name = p.get("choose_from") or p.get("counts_toward") or "unresolved"
            schedule.setdefault(index, []).append(f"{R.ELECTIVE_PREFIX}{name}")
    return schedule


def uts_schedule(plan):
    schedule = {}
    for p in plan["placements"]:
        schedule.setdefault(p["position"], []).append(p["code"])
    for slot in (plan.get("open_slots") or []):
        # A UTS open slot can be 6, 12 or 18 credit points, and a placeholder is
        # worth one subject. Emitting one placeholder for an 18cp slot understated
        # three plans by 6 and 12cp, which showed up as totals of 192 and 186
        # against a course that is 198. Expand it into subject-sized slots.
        n = max(1, round(slot.get("credit_points", 6) / 6))
        for _ in range(n):
            schedule.setdefault(slot["position"], []).append(
                f"{R.ELECTIVE_PREFIX}elective allowance")
    return schedule


def load_usyd_datasets(degrees):
    """One dataset per degree, loaded through datasets.py so the stores stay apart."""
    import datasets
    out = {}
    for degree in sorted(degrees):
        name = USYD_DEGREE_ALIAS.get(degree, degree)
        ds = datasets.load("USYD", name)
        out[degree] = {"units": ds.units, "blocks": ds.blocks}
    return out


def run(label, schedule, rules, data, do_replan):
    prereqs, coreqs, dependents = core.build_requisite_graph(data)
    violations = core.validate_schedule(rules, schedule, prereqs, coreqs, data)

    unknown = sorted({R.base_code(c) for codes in schedule.values() for c in codes
                      if not R.is_placeholder(c) and R.base_code(c) not in data["units"]})

    loads = {}
    for index in sorted(schedule):
        if R.is_real(index):
            loads[rules.position_label(index)] = core.period_credit_points(
                rules, schedule, index, data)

    result = {"plan": label, "institution": rules.institution,
              "positions": len(schedule),
              "placed": sum(1 for v in schedule.values() for c in v
                            if not R.is_placeholder(c)),
              "open_slots": sum(1 for v in schedule.values() for c in v
                                if c.startswith(R.ELECTIVE_PREFIX)),
              "credit_points": sum(rules.unit_cp(c, data)
                                   for v in schedule.values() for c in v),
              "violations": violations,
              "units_not_in_dataset": unknown,
              "period_loads": loads}

    if do_replan:
        placed = [c for v in schedule.values() for c in v if not R.is_placeholder(c)]
        ok = failed = 0
        errors = []
        # Violations the enrolment plan already has before anything is failed.
        # The published UTS sequences place four subjects outside the availability
        # their own pages state, reported as accepted findings rather than
        # corrected (Section 4.2.3). A repair inherits those and cannot remove
        # them, so counting them here would mark every repair of those two plans
        # as a failure for a discrepancy that was in the published sequence all
        # along. The plan-level check above already subtracts them; this makes the
        # replan check consistent with it. A repair is answerable for what it
        # breaks, not for what it inherited.
        inherited = set(violations)
        for code in placed:
            try:
                new = core.replan_after_failure(rules, schedule, code, data,
                                                prereqs, dependents, coreqs)
                after = [v for v in core.validate_schedule(
                    rules, new, prereqs, coreqs, data) if v not in inherited]
                if after:
                    failed += 1
                    if len(errors) < 3:
                        errors.append(f"{code}: replan left {len(after)} violation(s)")
                else:
                    ok += 1
            except Exception as e:
                failed += 1
                if len(errors) < 3:
                    errors.append(f"{code}: {type(e).__name__}: {str(e)[:90]}")
        result["replan"] = {"attempted": len(placed), "clean": ok,
                            "problems": failed, "examples": errors}
    return result


def main():
    do_replan = "--replan" in sys.argv
    results = []

    if os.path.exists(USYD_PLANS):
        plans = json.load(open(USYD_PLANS))["plans"]
        datasets_by_degree = load_usyd_datasets({p["degree"] for p in plans.values()})
        rules_obj = rules_usyd.USYDRules()
        for key, plan in plans.items():
            results.append(run(plan.get("title") or key, usyd_schedule(plan),
                               rules_obj, datasets_by_degree[plan["degree"]], do_replan))
    else:
        print(f"  [skip] {USYD_PLANS} not found")

    if os.path.exists(UTS_PLANS):
        data = json.load(open(P("uts_c09066_data.json")))
        rules_obj = rules_uts.UTSRules()
        for key, plan in json.load(open(UTS_PLANS))["plans"].items():
            results.append(run(key, uts_schedule(plan), rules_obj, data, do_replan))
    else:
        print(f"  [skip] {UTS_PLANS} not found")

    json.dump({"results": results}, open(OUT, "w"), indent=1)

    # Findings the UTS plans reproduce from the published sequences on purpose are
    # recorded in uts_plan_picks.json. Without reading that here, a deliberate
    # decision is indistinguishable from a defect in this report, which defeats the
    # point of recording it rather than suppressing it.
    # Matching on the prose turned out to be fragile: build_uts_default_plans.py
    # writes "needs 48260 OR 16912" and core.py writes the same finding with a
    # lowercase "or", so a substring test silently failed and a deliberate decision
    # was reported as a defect. Compare on the set of unit codes in the message
    # instead, which neither wording changes.
    # Both institutions declare accepted findings, in the same shape and read by
    # the same code. The file is per institution only because the plan keys are.
    accepted_spec = []
    for picks in (P("uts_plan_picks.json"), P("usyd_plan_picks.json")):
        if os.path.exists(picks):
            accepted_spec += json.load(open(picks)).get("accepted_findings") or []

    def key(text):
        # A credit-point threshold message names one unit and a number of credit
        # points, and the number is not a unit code. Matching on the five-digit
        # UTS codes first would make "36cp" look like a code, so the alphanumeric
        # form is tried whenever the message carries one.
        alpha = frozenset(re.findall(r"\b[A-Z]{2,5}\d{3,4}\b", text))
        return alpha or frozenset(re.findall(r"\b\d{5}\b", text))

    accepted_keys = {}
    for a in accepted_spec:
        accepted_keys.setdefault(a["major"], []).append((key(a["contains"]), a))

    for r in results:
        keep, accepted = [], []
        for v in r["violations"]:
            match = next((a for k, a in accepted_keys.get(r["plan"], [])
                          if k and k <= key(v)), None)
            (accepted if match else keep).append(v)
        r["violations"], r["accepted"] = keep, accepted

    by_inst = Counter(r["institution"] for r in results)
    print(f"ran {len(results)} plans: "
          + ", ".join(f"{n} {i}" for i, n in sorted(by_inst.items())))

    print(f"\n{'plan':54}{'units':>6}{'open':>6}{'cp':>6}{'violations':>12}")
    clean = 0
    for r in results:
        v = len(r["violations"])
        clean += (v == 0)
        print(f"{r['plan'][:52]:54}{r['placed']:>6}{r['open_slots']:>6}"
              f"{r['credit_points']:>5}cp{(v or 'none'):>12}")

    print(f"\nplans with no violations: {clean} / {len(results)}")
    for inst in sorted(by_inst):
        sub = [r for r in results if r["institution"] == inst]
        print(f"  {inst}: {sum(1 for r in sub if not r['violations'])} / {len(sub)}")

    acc = [(r, v) for r in results for v in r.get("accepted", [])]
    if acc:
        print(f"\naccepted findings, reproduced from the published plan on purpose: "
              f"{len(acc)}")
        for r, v in acc:
            print(f"  [{r['institution']}] {r['plan'][:44]}")
            print(f"      {v}")

    unknown = sorted({c for r in results for c in r["units_not_in_dataset"]})
    if unknown:
        print(f"\nunits placed but absent from their institution's dataset: "
              f"{len(unknown)}")
        print(f"  {unknown[:24]}")
        print("  These cannot be checked at all, so a clean result above is only as "
              "good as\n  this list is short. They are the USYD codes still awaiting "
              "a requisite scrape.")

    offenders, plans_with = Counter(), Counter()
    for r in results:
        for code in {v.split()[0] for v in r["violations"]}:
            plans_with[code] += 1
        for v in r["violations"]:
            offenders[v.split()[0]] += 1
    if offenders:
        print(f"\noffending units, worst first:")
        for code, n in offenders.most_common(12):
            print(f"  {code:12} {n:>3} violation(s) across {plans_with[code]} plan(s)")
        print(f"\n  every distinct violation message:")
        seen = set()
        for r in results:
            for v in r["violations"]:
                key = v.split(")", 1)[-1].strip()
                if key in seen:
                    continue
                seen.add(key)
                print(f"    [{r['institution']}] {v}")

    if do_replan:
        att = sum(r["replan"]["attempted"] for r in results)
        ok = sum(r["replan"]["clean"] for r in results)
        print(f"\nreplanning: {ok}/{att} failures replanned to a schedule that then "
              f"validates clean")
        for r in results:
            if r["replan"]["problems"]:
                print(f"  {r['plan'][:48]:50} {r['replan']['problems']} problem(s)")
                for e in r["replan"]["examples"]:
                    print(f"      {e}")

    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
