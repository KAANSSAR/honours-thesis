"""
Build the seven UTS C09066 default plans and validate them against the dataset.

    python3 build_uts_default_plans.py

Reads uts_c09066_data.json and uts_plan_picks.json, writes uts_default_plans.json.
No network access.

This is the UTS counterpart of build_default_plans.py and check_plans.py on the USYD
side, and it keeps the two institutions in separate stores: nothing here opens the
USYD data, and a requisite that cannot be resolved inside the UTS dataset is reported
as a violation rather than quietly looked up elsewhere.

A suggested sequence is not yet a plan. It carries choice groups, and a default plan
has to resolve them. Groups worded "from the following:" publish their options and are
resolved from uts_plan_picks.json. Groups worded "of options" are the handbook's 24cp
elective allowance and stay open, the same decision taken for USYD free electives: the
planner is supposed to fill those, so prefilling them would hide whether it can.

Everything is checked rather than assumed, because every fault found in this dataset so
far showed up as a plausible-looking value rather than an error:

  - the pick really is one of that group's published options
  - the subject is actually offered in the session it is placed in
  - per-session load, counting 0cp and 3cp subjects correctly
  - no subject placed twice in one plan
  - prerequisites satisfied in position order, as AND-of-ORs
  - corequisites placed no later than the subject that needs them
  - credit-point requisites met by the credit points scheduled before them

Positions mirror the USYD model so the two are directly comparable:
POSITIONS_PER_YEAR = 4, with sem1=0, winter_gap=1, sem2=2, summer_gap=3. UTS December
and January sit in the summer gap, July in the winter gap.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import json, os, sys
from collections import defaultdict

DATA = P("uts_c09066_data.json")
PICKS = P("uts_plan_picks.json")
OUT = P("uts_default_plans.json")

POSITIONS_PER_YEAR = 4
POS = {"sem1": 0, "winter_gap": 1, "sem2": 2, "summer_gap": 3}
SESSION_POSITION = {
    "Autumn": "sem1", "Spring": "sem2",
    "July": "winter_gap", "Winter": "winter_gap",
    "December": "summer_gap", "January": "summer_gap", "Summer": "summer_gap",
}


def position(year, session):
    kind = SESSION_POSITION.get(session)
    if kind is None:
        return None
    return (year - 1) * POSITIONS_PER_YEAR + POS[kind]


def is_open_group(entry, blocks):
    """Is this group part of the elective allowance, to be left for the planner?

    Two wordings mean the same thing. Most majors write "Select N credit points of
    options", with no options published. Electrical instead writes "from the
    following:" and names CBK90011, whose own title is "Electives Select 24 credit
    points of options:". Treating only the first wording as open left Electrical
    24cp short, which the credit-point check caught.

    So a group is open if it publishes no options, or if every option it does
    publish is a block that is itself an electives pool. The test is on the block's
    title rather than its code, so a renumbered pool still reads correctly.
    """
    if not entry.get("resolved_inline"):
        return True
    opts = entry["options"]
    if not opts or not all(o.get("is_block") for o in opts):
        return False
    return all((blocks.get(o["code"], {}).get("name") or "").strip().lower()
               .startswith("electives") for o in opts)


def group_option_codes(entry, blocks):
    """Every subject code this choice group permits, following blocks one level down."""
    codes, seen = set(), set()
    stack = []
    for o in entry["options"]:
        if o.get("is_block"):
            stack.append(o["code"])
        else:
            codes.add(o["code"])
    while stack:
        b = stack.pop()
        if b in seen:
            continue
        seen.add(b)
        rec = blocks.get(b)
        if not rec:
            continue
        codes.update(u["code"] for u in rec.get("units", []))
        stack.extend(rec.get("sub_blocks", []))
    return codes


def build(data, picks):
    """Turn each sequence into a list of placements, resolving the choice groups."""
    units, blocks = data["units"], data.get("blocks", {})
    plans, problems = {}, []

    for major, ents in data["sequences"].items():
        want = list(picks["picks"].get(major, []))
        placements, open_slots = [], []

        for e in ents:
            pos = position(e["year"], e["session"])
            if pos is None:
                problems.append(f"{major}: no position for session {e['session']!r}")
                continue
            common = {"year": e["year"], "session": e["session"], "position": pos}

            if e["kind"] == "fixed":
                placements.append({**common, "code": e["code"],
                                   "name": units.get(e["code"], {}).get("name", e["name"]),
                                   "credit_points": e["credit_points"],
                                   "origin": "sequence"})
                continue

            if is_open_group(e, blocks):
                open_slots.append({**common, "credit_points": e["credit_points"],
                                   "origin": "elective allowance, left for the planner",
                                   "text": e["text"]})
                continue

            # A published choice group: find the matching pick.
            match = None
            for i, p in enumerate(want):
                if (p["year"], p["session"], p["credit_points"]) == \
                        (e["year"], e["session"], e["credit_points"]):
                    match = want.pop(i)
                    break
            if match is None:
                problems.append(f"{major}: no pick for the {e['credit_points']}cp group "
                                f"in Y{e['year']} {e['session']} ({e['text'][:40]})")
                continue

            allowed = group_option_codes(e, blocks)
            chosen_cp = 0
            for code in match["choose"]:
                if allowed and code not in allowed:
                    problems.append(f"{major}: {code} is not an option of the group in "
                                    f"Y{e['year']} {e['session']}")
                cp = units.get(code, {}).get("credit_points")
                cp = 6 if cp is None else cp
                chosen_cp += cp
                placements.append({**common, "code": code,
                                   "name": units.get(code, {}).get("name", ""),
                                   "credit_points": cp, "origin": "pick",
                                   "forced": bool(match.get("forced")),
                                   "reason": match.get("reason", "")})
            if chosen_cp != e["credit_points"]:
                problems.append(f"{major}: pick for Y{e['year']} {e['session']} totals "
                                f"{chosen_cp}cp, group wants {e['credit_points']}cp")

        for p in want:
            problems.append(f"{major}: pick for Y{p['year']} {p['session']} "
                            f"({p['credit_points']}cp) matched no group in the sequence")

        placements.sort(key=lambda x: (x["position"], x["code"]))
        plans[major] = {"institution": "UTS", "course_code": data.get("course_code"),
                        "major": major, "placements": placements,
                        "open_slots": open_slots}
    return plans, problems


def validate(data, plans):
    """Check each plan against the dataset. Returns a list of findings per major."""
    units = data["units"]
    total_expected = data.get("total_credit_points", 198)
    findings = defaultdict(list)

    for major, plan in plans.items():
        pl = plan["placements"]
        at = {p["code"]: p["position"] for p in pl}
        placed_cp = sum(p["credit_points"] for p in pl)
        open_cp = sum(s["credit_points"] for s in plan["open_slots"])

        if placed_cp + open_cp != total_expected:
            findings[major].append(
                f"credit points: {placed_cp}cp placed + {open_cp}cp open "
                f"= {placed_cp + open_cp}cp, expected {total_expected}cp")

        seen = defaultdict(list)
        for p in pl:
            seen[p["code"]].append(f"Y{p['year']} {p['session']}")
        for code, where in seen.items():
            if len(where) > 1:
                findings[major].append(f"{code} placed twice: {', '.join(where)}")

        load = defaultdict(int)
        for p in pl:
            load[(p["year"], p["session"])] += p["credit_points"]
        for s in plan["open_slots"]:
            load[(s["year"], s["session"])] += s["credit_points"]
        for k in sorted(load):
            if load[k] > 27:
                findings[major].append(f"Y{k[0]} {k[1]} carries {load[k]}cp")

        for p in pl:
            u = units.get(p["code"])
            if u is None:
                findings[major].append(f"{p['code']} is not in the UTS dataset")
                continue

            offered = u.get("sessions_offered") or []
            if offered and f"{p['session']} session" not in offered:
                note = " (availability inferred)" if u.get("sessions_inferred") else ""
                findings[major].append(
                    f"{p['code']} is placed in {p['session']} but is offered in "
                    f"{', '.join(offered)}{note}")

            # Credit points completed before this position. Open elective slots
            # count: they are credit points the student will take, just not
            # subjects chosen yet. Leaving them out understated every cumulative
            # total by up to 24cp and produced five spurious findings on 41029.
            before_cp = (
                sum(q["credit_points"] for q in pl if q["position"] < p["position"]) +
                sum(s["credit_points"] for s in plan["open_slots"]
                    if s["position"] < p["position"]))

            if u.get("requisite_waived_for_some_courses"):
                # The handbook says this requisite may not apply depending on the
                # student's course, so a shortfall here is not evidence of a bad plan.
                continue

            def met(alt, limit):
                if alt["kind"] == "subject":
                    return alt["code"] in at and at[alt["code"]] < limit
                if alt["kind"] == "credit_points":
                    return before_cp >= alt["min_credit_points"]
                return True   # uncheckable text: reported separately, not failed here

            for req in u.get("prerequisite_requirements") or []:
                if any(met(a, p["position"]) for a in req):
                    continue
                # One requirement, one finding, however many alternatives it has.
                shown = " OR ".join(
                    a["code"] if a["kind"] == "subject"
                    else f"{a['min_credit_points']}cp" if a["kind"] == "credit_points"
                    else "course condition" for a in req)
                same = [a["code"] for a in req if a["kind"] == "subject"
                        and a["code"] in at and at[a["code"]] >= p["position"]]
                why = (f", and {', '.join(same)} "
                       f"{'is' if len(same) == 1 else 'are'} placed no earlier"
                       if same else ", none of which is in this plan")
                findings[major].append(
                    f"{p['code']} needs {shown} before Y{p['year']} {p['session']}{why}")

            for req in u.get("corequisite_requirements") or []:
                if any(a["code"] in at and at[a["code"]] <= p["position"] for a in req):
                    continue
                findings[major].append(
                    f"{p['code']} needs one of {' OR '.join(a['code'] for a in req)} "
                    f"as a corequisite, no later than Y{p['year']} {p['session']}")
    return findings


def main():
    for f in (DATA, PICKS):
        if not os.path.exists(f):
            sys.exit(f"{f} not found. Run it from the folder holding the UTS data.")
    data, picks = json.load(open(DATA)), json.load(open(PICKS))

    plans, problems = build(data, picks)
    findings = validate(data, plans)

    out = {"institution": "UTS", "course_code": data.get("course_code"),
           "course_name": data.get("course_name"),
           "total_credit_points": data.get("total_credit_points"),
           "source": data.get("source"), "source_url": data.get("source_url"),
           "open_slot_policy": picks.get("open_slot_policy"),
           "picks_source": {"file": os.path.basename(PICKS), "checked": picks.get("date_checked"),
                            "against": picks.get("source_checked")},
           "plans": plans}
    json.dump(out, open(OUT, "w"), indent=1)

    print(f"built {len(plans)} UTS default plans -> {OUT}\n")
    print(f"{'major':44}{'placed':>8}{'open':>7}{'total':>8}{'slots':>7}")
    for major in sorted(plans):
        p = plans[major]
        pc = sum(x["credit_points"] for x in p["placements"])
        oc = sum(x["credit_points"] for x in p["open_slots"])
        print(f"{major[:42]:44}{pc:>6}cp{oc:>5}cp{pc + oc:>6}cp"
              f"{len(p['open_slots']):>7}")

    forced = [(m, x) for m in sorted(plans) for x in plans[m]["placements"]
              if x.get("forced")]
    print(f"\npicks forced by session availability: {len(forced)}")
    for m, x in forced:
        print(f"  {m[:36]:38} Y{x['year']} {x['session']:8} {x['code']}  {x['name'][:34]}")

    if problems:
        print(f"\nbuild problems ({len(problems)}):")
        for p in problems:
            print(f"  {p}")
    else:
        print("\nbuild problems: none")

    # Accepted findings are faults in the published sequence that the plans
    # reproduce on purpose. They are printed under their own heading rather than
    # filtered out, so a genuinely new finding cannot hide among them.
    accepted_spec = picks.get("accepted_findings") or []
    unexpected, accepted = defaultdict(list), []
    for major in sorted(findings):
        for f in findings[major]:
            hit = next((a for a in accepted_spec
                        if a["major"] == major and a["contains"] in f), None)
            if hit:
                accepted.append((major, f, hit))
            else:
                unexpected[major].append(f)

    n = sum(len(v) for v in unexpected.values())
    print(f"\nunexpected findings: {n}")
    for major in sorted(unexpected):
        print(f"\n  {major}  ({len(unexpected[major])})")
        for f in unexpected[major]:
            print(f"    - {f}")
    if not n:
        print("  none: every plan passes every check apart from the accepted findings below")

    print(f"\naccepted findings, reproduced from the published sequence on purpose: "
          f"{len(accepted)}")
    for major, f, spec in accepted:
        print(f"\n  {major}")
        print(f"    - {f}")
        print(f"      decision: {spec['decision'][:160]}")
    missed = [a for a in accepted_spec
              if not any(s is a for _, _, s in accepted)]
    for a in missed:
        print(f"\n  [NOTE] accepted finding no longer occurs, so it can be removed "
              f"from {PICKS}: {a['major']} / {a['contains'][:60]}")


if __name__ == "__main__":
    main()
