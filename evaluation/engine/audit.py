"""
An independent, deliberately simple rule checker.

Written to be read rather than trusted. Every rule is checked in the most direct
way the data allows, with no solver and no constraint encoding, because the
previous checks failed not on the rules but on my encoding of them. If a rule here
is wrong, it should be wrong in a way that is obvious from reading twenty lines.

It checks the six things a plan has to satisfy:

  1. prerequisites   each group needs one member finished strictly earlier
  2. corequisites    finished earlier or taken alongside
  3. exclusions      two units that cannot be combined are not both present
  4. availability    a unit sits in a session it is actually offered in
  5. load            no teaching period exceeds the institution's ceiling
  6. thresholds      "N credit points of ... beforehand" is met

Conventions the engine uses and this follows:
  FAILED:X    occupies its period and charges its credit points, earns no credit
  ELECTIVE:n  an unfilled slot, charges credit points, counts toward an
              unrestricted threshold, and toward a level threshold only when its
              block name promises that level

Usage:
    python3 evaluation/engine/audit.py [--replan] [--verbose]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import os
import re
import sys


def level_of(code):
    m = re.match(r"[A-Z]{2,5}(\d)\d{2,3}$", code)
    return int(m.group(1)) * 1000 if m else None


def slot_at_least(placeholder, want_level, data):
    """
    Is every unit this unfilled slot could hold at or above `want_level`?

    The slot's label lists the blocks a student may choose from, separated by a
    pipe. A block whose recorded members are all at or above the level settles
    it. A block with no recorded members cannot, because its requirement is
    published as open rather than as a list.
    """
    name = placeholder.split(":", 1)[-1]
    if f"{want_level // 1000}000+" in name:
        return True
    blocks = data.get("blocks") or {}
    for part in name.split("|"):
        members = (blocks.get(part.strip()) or {}).get("members") or ()
        if not members:
            continue
        got = [level_of(m) for m in members]
        if got and all(g is not None and g >= want_level for g in got):
            return True
    return False


def audit(rules, schedule, data, R, core):
    """Return a list of violation strings. Empty means the plan satisfies every rule."""
    units = data["units"]
    out = []

    # where each unit sits, excluding failed attempts and unfilled slots
    where = {}
    for index, codes in schedule.items():
        for c in codes:
            if not str(c).startswith((R.FAILED_PREFIX, R.ELECTIVE_PREFIX)):
                where[c] = index

    def cp(c):
        base = R.base_code(str(c))
        if str(c).startswith(R.ELECTIVE_PREFIX):
            return rules.elective_slot_cp
        return units.get(base, {}).get("credit_points", 0) or 0

    def earned_before(index, want_level=None, want_prefix=None, excluded=()):
        """Credit points that count as completed strictly before `index`."""
        total = 0
        for i, codes in schedule.items():
            if i >= index:
                continue
            for c in codes:
                c = str(c)
                if c.startswith(R.FAILED_PREFIX):
                    continue                       # a failed attempt earns nothing
                if c.startswith(R.ELECTIVE_PREFIX):
                    # An unfilled slot counts toward an unrestricted threshold
                    # always: it is credit the student will take, merely not yet
                    # chosen. For a level threshold it counts only where it cannot
                    # be filled below that level, which is decided from the blocks
                    # it draws from rather than from the engine's own helper, so
                    # the two remain separate readings of the same rule. A block
                    # the dataset holds no members for, such as Free Electives,
                    # settles nothing and is passed over.
                    if want_level is None:
                        total += rules.elective_slot_cp
                    elif slot_at_least(c, want_level, data):
                        total += rules.elective_slot_cp
                    continue
                if want_prefix and not c.startswith(want_prefix):
                    continue
                if want_level is not None:
                    lv = level_of(c)
                    if lv is None or lv < want_level:
                        continue
                if any(re.fullmatch(p, c) for p in excluded):
                    continue
                total += cp(c)
        return total

    for code, index in sorted(where.items(), key=lambda kv: kv[1]):
        rec = units.get(R.base_code(code))
        if not rec:
            out.append(f"{code}: not in the {rules.institution} dataset")
            continue
        at = rules.position_label(index)

        # UTS notes on many subjects that the requisites may not apply depending
        # on the student's course. The engine honours that flag, so a requisite
        # check here would report a finding the institution has already waived.
        waived = bool(rec.get("requisite_waived_for_some_courses"))

        # 1. prerequisites
        # Prefer the typed requirements, which keep alternatives that are not unit
        # codes. A group like "48521 OR SMJ10186 24cp Engineering Sub-major" has a
        # route the dataset cannot verify, and `prerequisite_groups` drops it,
        # leaving what looks like a hard requirement on 48521. Reading the groups
        # alone reported four violations that were not violations.
        typed = rec.get("prerequisite_requirements")
        groups, unverifiable = [], []
        if typed:
            for grp in typed:
                codes, other = [], False
                for alt in grp:
                    if isinstance(alt, dict) and alt.get("kind") == "subject" and alt.get("code"):
                        codes.append(alt["code"])
                    else:
                        other = True
                groups.append(codes)
                unverifiable.append(other)
        else:
            groups = [list(g) for g in (rec.get("prerequisite_groups") or []) if g]
            if not groups and rec.get("prerequisite_codes"):
                groups = [list(rec["prerequisite_codes"])]
            unverifiable = [False] * len(groups)

        for g, has_other in zip(([] if waived else groups), unverifiable):
            if has_other:
                continue              # an alternative exists that cannot be checked
            scheduled = [p for p in g if p in where]
            if not scheduled:
                continue              # satisfied outside the plan, or not recorded
            if not any(where[p] < index for p in scheduled):
                out.append(f"{code} at {at}: needs one of {g} finished earlier; "
                           f"in this plan {[(p, rules.position_label(where[p])) for p in scheduled]}")

        # 2. corequisites, satisfied at or before this unit.
        #
        # `corequisite_codes` is a flat list and is read as alternatives: any one
        # satisfies it. Reading that list as a conjunction is the fault Section
        # 3.3.2 records. But the flat list loses the AND/OR structure, and one
        # subject really does need both: 48321 Engineering Mechanics states
        # "33130c Mathematics 1 AND 68037c Physical Modelling", and the typed
        # `corequisite_requirements` keeps that as two groups. Where the typed
        # form exists it is authoritative, with AND across groups and OR within,
        # which is what the engine already does; reading only the flat list made
        # this checker the more permissive of the two on exactly this subject.
        groups = rec.get("corequisite_requirements")
        if groups:
            for group in groups:
                alts = [a.get("code") for a in group if a.get("code")]
                present = [q for q in alts if q in where]
                if not present:
                    continue              # satisfied outside the plan
                if not any(where[q] <= index for q in present):
                    out.append(f"{code} at {at}: needs one of {alts} at or before; "
                               f"all sit later")
        else:
            coreq = [q for q in (rec.get("corequisite_codes") or []) if q in where]
            if coreq and not any(where[q] <= index for q in coreq):
                out.append(f"{code} at {at}: needs one of {coreq} at or before; "
                           f"all sit later")

        # 3. exclusions
        for x in (rec.get("prohibition_codes") or rec.get("antirequisite_codes") or []):
            if x in where:
                out.append(f"{code} at {at}: cannot be combined with {x} "
                           f"({rules.position_label(where[x])})")

        # 4. availability
        offered = rec.get("sessions_offered")
        if offered and not rules.offered_in(code, index, data):
            out.append(f"{code} at {at}: not offered then (runs in {offered})")

        # 5. thresholds
        req = rec.get("credit_point_requirement")
        reqs = [req] if isinstance(req, dict) else list(
            rec.get("credit_point_requirements") or [])
        reqs = [r for r in reqs if r and (r.get("min_credit_points") or 0)]
        if reqs and not waived:
            best = min(reqs, key=lambda r: r["min_credit_points"])
            need = best["min_credit_points"]
            got = earned_before(index, best.get("min_level"), best.get("prefix"),
                                best.get("excluded_patterns") or ())
            if got < need:
                lv = f" of {best['min_level']}-level or higher" if best.get("min_level") else ""
                out.append(f"{code} at {at}: needs {need}cp{lv} beforehand, has {got}cp")

    # 6. a retake cannot sit at or before the attempt that failed. Mutation
    # testing found nothing here was checking this: removing the rule from the
    # engine left the audit unable to tell the difference.
    for index, codes in schedule.items():
        for c in codes:
            if not str(c).startswith(R.FAILED_PREFIX):
                continue
            base = R.base_code(str(c))
            if base in where and where[base] <= index:
                out.append(f"{base} retaken at {rules.position_label(where[base])}, "
                           f"at or before the attempt that failed at "
                           f"{rules.position_label(index)}")

    # 7. load per teaching period, intensives charged to the period that owns them
    charged = {}
    for index, codes in schedule.items():
        for c in codes:
            if str(c).startswith(R.ELECTIVE_PREFIX):
                owner = index
            else:
                owner = core.load_index(rules, index, R.base_code(str(c)), data)
            charged[owner] = charged.get(owner, 0) + cp(c)
    for owner, total in sorted(charged.items()):
        if total > rules.max_cp_per_period:
            out.append(f"{rules.position_label(owner)}: {total}cp exceeds the "
                       f"{rules.max_cp_per_period}cp ceiling")

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--replan", action="store_true",
                    help="also audit every single-failure repair")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    import api, core
    import rules as R

    engine = api.Engine()
    print("=" * 72)
    print("PART 1 - the 51 enrolment plans as published")
    print("=" * 72)
    plan_bad = 0
    for inst in ("USYD", "UTS"):
        for key, plan in engine.plans.get(inst, {}).items():
            rules = engine.rules[inst]
            data = engine._dataset_for(inst, plan)
            sched = engine._schedule_for(inst, plan)
            v = audit(rules, sched, data, R, core)
            if v:
                plan_bad += 1
                print(f"\n[{inst}] {key}  ({len(v)} finding(s))")
                for line in v[:6 if not args.verbose else len(v)]:
                    print(f"    {line}")
                if not args.verbose and len(v) > 6:
                    print(f"    ... {len(v) - 6} more")
    total_plans = sum(len(p) for p in engine.plans.values())
    print(f"\nplans with findings: {plan_bad} / {total_plans}")

    if not args.replan:
        return

    print("\n" + "=" * 72)
    print("PART 2 - every single-failure repair")
    print("=" * 72)
    clean = broken = failed_to_plan = 0
    kinds = {}
    worst = []
    for inst in ("USYD", "UTS"):
        for key, plan in engine.plans.get(inst, {}).items():
            rules = engine.rules[inst]
            data = engine._dataset_for(inst, plan)
            sched = engine._schedule_for(inst, plan)
            base = set(audit(rules, sched, data, R, core))
            prereqs, coreqs, dependents = engine._graph(inst, plan)
            for code in sorted({c for v in sched.values() for c in v
                                if not R.is_placeholder(c)}):
                try:
                    new = core.replan_after_failure(rules, sched, code, data,
                                                    prereqs, dependents, coreqs)
                except Exception as exc:
                    failed_to_plan += 1
                    continue
                v = [x for x in audit(rules, new, data, R, core) if x not in base]
                if v:
                    broken += 1
                    for line in v:
                        kind = ("load" if "ceiling" in line else
                                "availability" if "not offered" in line else
                                "threshold" if "beforehand" in line else
                                "corequisite" if "corequisite" in line else
                                "exclusion" if "combined" in line else "prerequisite")
                        kinds[kind] = kinds.get(kind, 0) + 1
                    if len(worst) < 6:
                        worst.append((inst, key, code, v[0]))
                else:
                    clean += 1
    print(f"repairs with no NEW rule violation : {clean}")
    print(f"repairs that introduce a violation : {broken}")
    print(f"scenarios the engine could not plan: {failed_to_plan}")
    if kinds:
        print(f"\nby rule: {kinds}")
        print("\nexamples:")
        for inst, key, code, line in worst:
            print(f"  [{inst}] fail {code} in {key[:40]}")
            print(f"      {line}")


if __name__ == "__main__":
    main()
