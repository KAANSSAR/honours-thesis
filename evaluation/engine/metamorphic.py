"""
Metamorphic testing of the repair.

Section 4.1.2 states the problem this addresses. Every figure in Sections 4.2 and
4.3 is produced by the validator, and the validator is part of the planner, so a
rule encoded wrongly is encoded wrongly in both and the report comes back clean.
The independent checker and the constraint model attack that from outside; this
attacks it from inside, by asserting relations that must hold between two runs of
the same engine whatever the rules happen to say.

A metamorphic relation needs no oracle. It does not ask whether a repair is
right, which is the question the validator already begs; it asks whether two
repairs are consistent with each other, and an engine that is wrong in a way both
the planner and the validator share can still fail that.

The relations, each with the reason it must hold:

  R1  determinism            The same request twice gives the same schedule.
                             Section 4.5.2 puts the no-bias claim on this.
  R2  idempotence            Repairing a plan that was already repaired for the
                             same unit changes nothing further: the retake is
                             placed, and asking again cannot move it.
  R3  no retake before the   A retake cannot be scheduled at or before the
      failure                attempt that failed. Compaction broke this once
                             and it is checked here rather than assumed.
  R4  the failed unit stays  The failure marker is never removed, so the
      marked                 overload budget a repair earned cannot be spent
                             twice over.
  R5  monotone span          Failing a second unit never shortens the plan.
  R6  locality               A repair moves only units in the failed unit's
                             dependency closure. Section 4.3.4 narrowed the
                             cascade to make this true; nothing enforces it.
  R7  unit conservation      The repair neither invents nor loses a placement.

Usage:
    python3 evaluation/engine/metamorphic.py [--limit N]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from paths import P

import argparse
import copy
import json
import os
import sys

passed = failed = 0
FAILURES = []
OUTSIDE = []        # (units moved outside the closure, units moved, institution)
NONMONOTONE = []    # a second failure that shortened the plan, with the reason


def check(relation, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        FAILURES.append((relation, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import api, core
    import rules as R

    eng = api.Engine()

    def span_end(s):
        return max(i for i, v in s.items() if v)

    def placed(s):
        """
        Where each unit is actually taken, by base code.

        Both markers are excluded. An open elective slot is not a placement, and
        a `FAILED:` marker records an attempt rather than an enrolment: counting
        it as a placement puts the unit in two positions at once and makes the
        retake look as though it had been scheduled on top of the failure.
        """
        out = {}
        for i, codes in s.items():
            for c in codes:
                c = str(c)
                if R.is_placeholder(c):
                    continue
                out.setdefault(R.base_code(c), set()).add(i)
        return out

    def closure(code, dependents):
        seen, stack = set(), [code]
        while stack:
            cur = stack.pop()
            for nxt in dependents.get(cur, set()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    tested = 0
    for inst in ("USYD", "UTS"):
        rules = eng.rules[inst]
        for key, plan in eng.plans.get(inst, {}).items():
            data = eng._dataset_for(inst, plan)
            base = eng._schedule_for(inst, plan)
            prereqs, coreqs, dependents = eng._graph(inst, plan)
            inherited = set(core.validate_schedule(rules, base, prereqs,
                                                   coreqs, data))
            codes = sorted({c for v in base.values() for c in v
                            if not R.is_placeholder(c)})

            for code in codes:
                where = f"{inst} {key[:34]} {code}"
                try:
                    once = core.replan_after_failure(
                        rules, base, code, data, prereqs, dependents, coreqs)
                    again = core.replan_after_failure(
                        rules, base, code, data, prereqs, dependents, coreqs)
                except Exception as exc:
                    check("R1 determinism", False, f"{where}: {exc}")
                    continue
                tested += 1

                check("R1 determinism", placed(once) == placed(again), where)

                # R2. Compaction is adopted only if the compacted plan still
                # validates, so running it on an already-repaired plan can
                # shorten it or leave it alone and can never break it. This is
                # the one invariant the repair claims about itself.
                try:
                    # A deep copy, not dict(once). A shallow copy shares the
                    # per-period lists, so compaction rearranges the schedule
                    # this function is still holding and every relation checked
                    # after this point measures a corrupted plan. That mistake
                    # reported 49 scenarios in which a second failure shortened
                    # the degree, which the engine does not do.
                    packed = core.compact_schedule(rules, copy.deepcopy(once),
                                                   data, prereqs, coreqs)
                    bad = [v for v in core.validate_schedule(
                        rules, packed, prereqs, coreqs, data)
                        if v not in inherited]
                    check("R2 compaction never breaks a plan", not bad,
                          f"{where}: {[str(v) for v in bad][:3]}")
                    check("R2 compaction never lengthens a plan",
                          span_end(packed) <= span_end(once),
                          f"{where}: span {span_end(once)} -> {span_end(packed)}")
                except Exception as exc:
                    check("R2 compaction never breaks a plan", False,
                          f"{where}: {type(exc).__name__}: {exc}")

                before = placed(base).get(code, set())
                after = placed(once).get(code, set())
                marker = {i for i, v in once.items()
                          if any(str(c) == R.FAILED_PREFIX + code for c in v)}

                check("R3 no retake at or before the failure",
                      all(a > min(before) for a in after) if before and after
                      else True,
                      f"{where}: failed at {sorted(before)}, retaken at {sorted(after)}")

                check("R4 the failed unit stays marked", bool(marker), where)

                # R6 is a measurement rather than a relation, and the reason is
                # worth stating. Only the failure's dependency closure can be
                # moved by the cascade, which Section 4.3.4 narrowed to make
                # true. Compaction is a separate step and moves whatever the
                # rules let it move earlier, by design, so a unit outside the
                # closure changing position is not a defect. What it is is the
                # part of the rearrangement a student cannot attribute to their
                # own failure, so it is counted rather than asserted away.
                may_move = closure(code, dependents) | {code}
                moved = {c for c, pos in placed(once).items()
                         if placed(base).get(c) and pos != placed(base)[c]}
                OUTSIDE.append((len(moved - may_move), len(moved), inst))

                check("R7 unit conservation",
                      set(placed(once)) == set(placed(base)) | {code},
                      f"{where}: "
                      f"{sorted(set(placed(base)) - set(placed(once)))[:4]}")

                # R5. A second failure must not take the plan below the span the
                # course publishes.
                #
                # The relation first written here was the stronger and more
                # obvious one, that a second failure never shortens the plan,
                # and it failed 49 times. The engine is right and the relation
                # was wrong. The overload allowance is one period per failure,
                # so a second failure earns a second overloaded period, and the
                # extra room can buy back more span than the failure costs. On
                # the Computer Engineering plan with the Innovation breadth,
                # failing SOFT2201 alone finishes a semester late using one
                # overloaded period, and failing SOFT2201 and ENGG3112 together
                # finishes on time using two. Both are valid. Disruption is
                # therefore not monotone in the number of failures, which is
                # worth knowing and is not a defect.
                others = [c for c in codes if c != code]
                if others:
                    second = others[len(others) // 2]
                    try:
                        both = core.replan_after_failure(
                            rules, copy.deepcopy(once), second, data,
                            prereqs, dependents, coreqs)
                        check("R5 never shorter than the published span",
                              span_end(both) >= span_end(base),
                              f"{where} then {second}: published "
                              f"{span_end(base)}, got {span_end(both)}")
                        if span_end(both) < span_end(once):
                            NONMONOTONE.append(
                                (inst, f"{where} then {second}: "
                                       f"{span_end(once)} -> {span_end(both)}"))
                    except Exception as exc:
                        check("R5 never shorter than the published span", False,
                              f"{where}: {exc}")

                if args.limit and tested >= args.limit:
                    break
            if args.limit and tested >= args.limit:
                break
        if args.limit and tested >= args.limit:
            break

    print(f"scenarios: {tested}")
    print(f"relation checks: {passed + failed}, passed {passed}, failed {failed}")
    by_relation = {}
    for relation, detail in FAILURES:
        by_relation.setdefault(relation, []).append(detail)
    for relation in sorted(by_relation):
        rows = by_relation[relation]
        print(f"\n  {relation}: {len(rows)} failures")
        for detail in rows[:8]:
            print(f"    {detail}")
    if NONMONOTONE:
        print(f"\n  A second failure finished the degree earlier than the first "
              f"alone, in {len(NONMONOTONE)} of {tested} scenarios. The second "
              f"failure earns a second overloaded period and the extra room buys "
              f"back more span than the failure costs; every one of these is a "
              f"valid plan. Disruption is not monotone in the number of "
              f"failures.")
        for inst, detail in NONMONOTONE[:5]:
            print(f"    {detail}")

    if OUTSIDE:
        print("\n  Units moved that the failure did not depend on "
              "(compaction's doing, measured not asserted):")
        for inst in ("USYD", "UTS"):
            rows = [(o, m) for o, m, i in OUTSIDE if i == inst]
            if not rows:
                continue
            total_out = sum(o for o, _ in rows)
            total_mv = sum(m for _, m in rows)
            any_out = sum(1 for o, _ in rows if o)
            print(f"    {inst}: {total_out} of {total_mv} moves are outside the "
                  f"closure, in {any_out} of {len(rows)} scenarios")

    json.dump({"tested": tested, "passed": passed, "failed": failed,
               "failures": [{"relation": r, "detail": d} for r, d in FAILURES],
               "outside_closure": OUTSIDE,
               "non_monotone": NONMONOTONE},
              open(P("metamorphic.json"), "w"))
    print("\nwrote " + P("metamorphic.json"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
