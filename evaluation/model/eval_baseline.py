"""
Section 4.5: the language-model-alone baseline.

The same model answers the same requests with the same rule data available and
no engine, which is the comparison Section 4.1.5 specifies. It isolates the
architecture rather than the model: Qwen3-8B is on both sides, decoding at
temperature zero on both sides, and the only difference is whether a
deterministic engine decides the plan or the model does.

The model is asked for a schedule as JSON so that the same validator can judge
it. That is a generous framing and deliberately so. Free prose would fail for
reasons of format rather than of planning, and a baseline that loses on a
technicality proves nothing; asking for JSON gives the model the best chance the
comparison allows.

Four things are counted, in the order a plan can fail:

  unparseable   no schedule could be read out of the reply at all
  invented      a unit code that is not in the dataset
  incomplete    the schedule omits units the degree requires, or misstates the
                total credit points
  invalid       the validator reports a rule violation

A plan can fail several ways at once and is counted under each, because the
interesting figure is how often each kind of failure occurs rather than a single
pass rate.

Usage:
    python3 eval_baseline.py --model qwen3:8b --out baseline_results.json
    python3 eval_baseline.py --dry-run     # print one prompt and stop
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import json
import re
import sys

import api
import core
import llm_layer as L
import rules as R
from paths import P

PROMPT = """You are an academic adviser. Produce a degree plan.

DEGREE: {degree}
TOTAL CREDIT POINTS REQUIRED: {total}
TEACHING PERIODS AVAILABLE: {periods}
MAXIMUM CREDIT POINTS PER PERIOD: {max_cp}

UNITS, with their rules:
{units}

STUDENT: {message}

Reply with JSON only, no commentary, in exactly this shape:

{{"plan": [
  {{"period": 0, "units": ["CODE1", "CODE2"]}},
  {{"period": 1, "units": ["CODE3"]}}
]}}

Period numbers start at 0 and count teaching periods in order. Use only unit
codes from the list above, and schedule each one exactly once.

Every unit's prerequisites must be scheduled in an earlier period than the unit
itself. A corequisite must be in the same period or an earlier one. A unit may
only be placed in a period whose session it is offered in. No period may exceed
the maximum credit points given above."""


def unit_lines(engine, institution, plan_key, limit=90):
    """
    The rule data the model is given: the same facts the engine reads.

    The prerequisite is rendered by the institution's own rule set, through
    `describe_prereq`, rather than read out of a dataset field here. The two have
    to agree or the comparison is not the one Section 4.1.5 specifies, and they
    drifted once already: this function used to print `prerequisite_groups`,
    which keeps only the subject alternatives of a UTS requirement, so eight
    requirements on UTS Software lost their credit-point alternative and 41029
    was presented as needing a subject that is not in the plan at all. The
    validator was reading the typed form and judging correctly throughout, so
    the effect was to hand the model an impossible constraint and then score it
    against the real one.
    """
    plan = engine.plans[institution][plan_key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    schedule = engine._schedule_for(institution, plan)
    codes = sorted({R.base_code(str(c)) for v in schedule.values() for c in v
                    if not R.is_placeholder(c)})
    out = []
    for code in codes[:limit]:
        unit = data["units"].get(code, {})
        bits = [f"{code} ({unit.get('credit_points', 6)}cp)"]
        prereq = rules.describe_prereq(code, data)
        if prereq:
            bits.append(f"prereq: {prereq}")
        coreq = rules.describe_coreq(code, data)
        if coreq:
            bits.append(f"coreq, same period or earlier: {coreq}")
        offered = unit.get("sessions_offered")
        if offered:
            bits.append("offered: " + ", ".join(offered))
        out.append("  " + "; ".join(bits))
    return "\n".join(out), codes, data, schedule


def check_prompt_matches_rules(engine, institution, plan_key, codes, data):
    """
    Every requirement the validator will enforce must appear in the prompt.

    This is the guard the drift above needed. For each unit it counts the
    requirement groups the institution's `prereq_satisfied` iterates, keeping
    only those holding an alternative that function can actually check, and
    compares that with the number of groups `describe_prereq` renders. A
    requirement the validator enforces and the prompt omits makes the baseline
    look worse than it is; one the prompt states and the validator ignores makes
    it look better. Either way the comparison is no longer the one
    Section 4.1.5 specifies, so this returns the mismatches and `main` refuses
    to run on a non-empty list rather than producing a figure quietly.
    """
    rules = engine.rules[institution]
    problems = []
    for code in codes:
        unit = data["units"].get(code, {})
        if unit.get("requisite_waived_for_some_courses"):
            continue

        typed = unit.get("prerequisite_requirements")
        if typed:
            enforced = sum(
                1 for req in typed
                if any(a.get("kind") in ("subject", "credit_points") for a in req))
        elif unit.get("prerequisite_groups"):
            enforced = len(unit["prerequisite_groups"])
        elif unit.get("prerequisite_codes"):
            enforced = 1
        elif unit.get("credit_point_requirement"):
            enforced = 1
        else:
            enforced = 0

        described = rules.describe_prereq(code, data)
        shown = described.count("(") if described else 0
        if described and "(" not in described:
            shown = 1                      # a bare threshold, rendered without brackets

        if shown != enforced:
            problems.append(f"{institution} {code}: validator enforces {enforced} "
                            f"prerequisite group(s), prompt shows {shown} "
                            f"({described!r})")

        # Corequisites. The first version of this guard checked prerequisites
        # alone and then reported that the prompt and the validator agreed,
        # which was false: COMP2017's corequisite was enforced on all seven
        # Sydney plans and stated in none of them. A guard that covers one of
        # the rules it is meant to cover is worse than no guard, because it is
        # believed.
        groups = unit.get("corequisite_requirements")
        if groups:
            co_enforced = sum(1 for g in groups if any(a.get("code") for a in g))
        elif unit.get("corequisite_codes"):
            co_enforced = 1
        else:
            co_enforced = 0
        co_described = rules.describe_coreq(code, data)
        co_shown = co_described.count("(") if co_described else 0
        if co_shown != co_enforced:
            problems.append(f"{institution} {code}: validator enforces "
                            f"{co_enforced} corequisite group(s), prompt shows "
                            f"{co_shown} ({co_described!r})")

        # Two further rules the validator applies. Neither can fire for a model
        # restricted to this plan's own unit list, because the published plan
        # satisfies both, but that is a property of the material rather than of
        # the harness, so it is checked rather than assumed.
        if unit.get("immediate_successor_of"):
            problems.append(f"{institution} {code}: validator enforces an "
                            f"ordering rule the prompt does not state")
        banned = set(unit.get("prohibition_codes") or []) & set(codes)
        if banned:
            problems.append(f"{institution} {code}: cannot be taken with "
                            f"{sorted(banned)}, which the prompt does not state")
    return problems


def parse_plan(reply):
    """Pull a {period: [codes]} schedule out of the model's reply, or None."""
    if not reply:
        return None
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    rows = obj.get("plan")
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            period = int(row.get("period"))
        except (TypeError, ValueError):
            continue
        units = [str(u).strip().upper() for u in (row.get("units") or [])
                 if str(u).strip()]
        if units:
            out.setdefault(period, []).extend(units)
    return out or None


def judge(engine, institution, plan_key, produced, known, data, reference):
    """Score one produced plan against the dataset and the validator."""
    rules = engine.rules[institution]
    plan = engine.plans[institution][plan_key]
    prereqs, coreqs, _ = engine._graph(institution, plan)

    invented = sorted({c for v in produced.values() for c in v if c not in known})
    wanted = {R.base_code(str(c)) for v in reference.values() for c in v
              if not R.is_placeholder(c)}
    got = {c for v in produced.values() for c in v}
    missing = sorted(wanted - got)

    # Periods as the model numbered them are teaching periods in order; the
    # engine counts positions including the intensive windows, so they are
    # mapped onto the real teaching positions before validating.
    teaching = [i for i in sorted(reference) if i % R.POSITIONS_PER_YEAR in (0, 2)]
    schedule = {}
    for period, codes in sorted(produced.items()):
        if period < len(teaching):
            schedule.setdefault(teaching[period], []).extend(
                [c for c in codes if c in known])

    # The degree's unfilled elective allowance is carried over from the published
    # plan rather than asked of the model. It is credit the student takes either
    # way, and leaving it out would charge the baseline for a shortfall the
    # question never asked it to cover: without these, UTS Software reads as
    # 174 credit points and 41029's 144 credit point threshold fails on a plan
    # that is actually correct.
    for index, codes in reference.items():
        for code in codes:
            if R.is_placeholder(code):
                schedule.setdefault(index, []).append(code)

    total = sum(rules.unit_cp(c, data) for v in schedule.values() for c in v)

    violations = core.validate_schedule(rules, schedule, prereqs, coreqs, data) \
        if schedule else ["no placements survived mapping"]

    return {"invented_codes": invented,
            "missing_units": missing,
            "total_credit_points": total,
            "violations": violations,
            "violation_count": len(violations)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=L.MODEL_NAME)
    ap.add_argument("--timeout", type=int, default=600,
                    help="seconds per request. Higher than the default elsewhere "
                         "because a whole plan is a much longer reply than an "
                         "intake profile or a two-sentence explanation.")
    ap.add_argument("--num-predict", type=int, default=2048,
                    help="cap on reply length in tokens. A correct plan for "
                         "these degrees is well under 1,000, so this is "
                         "generous; a reply that exceeds it is counted as "
                         "unparseable rather than left to hang. 0 removes it.")
    ap.add_argument("--retries", type=int, default=1,
                    help="retry a request once if the model does not answer in "
                         "time. A timeout is a property of the machine, not of "
                         "the model's planning, so it should not be counted as "
                         "a planning failure without a second chance.")
    ap.add_argument("--profiles", default=P("profiles.json"))
    ap.add_argument("--out", default=P("baseline_results.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print one prompt and exit, making no model calls")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many requests. Use --limit 1 to check "
                         "the model answers at all before committing to the "
                         "full set.")
    args = ap.parse_args()

    engine = api.Engine()
    profiles = json.load(open(args.profiles, encoding="utf-8"))

    # One plan per institution, asked once per persona.
    targets = [("UTS", "Software Engineering major"),
               ("USYD", sorted(k for k in engine.plans["USYD"]
                               if k.startswith("Software"))[0])]

    # The prompt and the judge must describe the same rules before a single
    # model call is made, because a figure produced under a mismatch is not a
    # baseline, it is an artefact of the harness.
    mismatches = []
    for inst, key in targets:
        _, codes, data, _ = unit_lines(engine, inst, key)
        mismatches += check_prompt_matches_rules(engine, inst, key, codes, data)
    if mismatches:
        print("The prompt and the validator disagree about these units, so the "
              "comparison would not be the one Section 4.1.5 specifies:\n")
        for line in mismatches:
            print(f"  {line}")
        sys.exit("\nrefusing to run until the prompt states the rules the "
                 "validator enforces")
    print(f"prompt and validator agree on the requirements of every unit in "
          f"{len(targets)} plans\n")

    if args.dry_run:
        inst, key = targets[0]
        units, codes, data, ref = unit_lines(engine, inst, key)
        rules = engine.rules[inst]
        total_cp = sum(rules.unit_cp(c, data) for v in ref.values() for c in v)
        teaching = [i for i in sorted(ref) if i % R.POSITIONS_PER_YEAR in (0, 2)]
        print(PROMPT.format(
            degree=f"{inst} {key}", total=total_cp, periods=len(teaching),
            max_cp=rules.max_cp_per_period, units=units,
            message=profiles["personas"][0]["prefix"] + "Give me a full plan."))
        print(f"\n[dry run: {len(codes)} units listed, no model called]")
        return

    rows, counts = [], {"no_reply": 0, "unparseable": 0, "invented": 0,
                        "incomplete": 0, "invalid": 0, "clean": 0, "total": 0}

    def save():
        """Written after every request. An hour of results should not depend on
        the last one surviving, which is the same lesson the consistency harness
        taught."""
        json.dump({"counts": counts, "rows": rows}, open(args.out, "w"),
                  indent=1, default=str)
    for inst, key in targets:
        if args.limit and counts["total"] >= args.limit:
            break
        units, codes, data, ref = unit_lines(engine, inst, key)
        rules = engine.rules[inst]
        total_cp = sum(rules.unit_cp(c, data) for v in ref.values() for c in v)
        teaching = [i for i in sorted(ref) if i % R.POSITIONS_PER_YEAR in (0, 2)]
        known = set(codes)

        for persona in profiles["personas"]:
            if args.limit and counts["total"] >= args.limit:
                break
            message = (persona["prefix"] + "Give me a full plan for this degree.").strip()
            backend = L.OllamaBackend(model=args.model, timeout=args.timeout,
                                      num_predict=args.num_predict or None)
            counts["total"] += 1
            row = {"institution": inst, "plan": key, "persona": persona["id"]}
            prompt = PROMPT.format(
                degree=f"{inst} {key}", total=total_cp, periods=len(teaching),
                max_cp=rules.max_cp_per_period, units=units, message=message)

            reply, last = None, None
            for attempt in range(args.retries + 1):
                try:
                    reply = backend.generate(prompt)
                    # Why the reply looks the way it does, from the server's own
                    # counters, so an empty one is attributable in the record.
                    row["meta"] = dict(getattr(backend, "last_meta", {}) or {})
                    break
                except Exception as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    if attempt < args.retries:
                        print(f"  {inst:5} {persona['id']:14} "
                              f"no answer in {args.timeout}s, retrying once")
            if reply is None:
                row["error"] = last
                row["attempts"] = args.retries + 1
                rows.append(row)
                save()
                print(f"  {inst:5} {persona['id']:14} ERROR {(last or '')[:50]}")
                continue

            row["reply"] = reply
            # An empty reply is not a bad plan. Counting it as unparseable would
            # charge the model for a planning failure when it did not plan at
            # all, and the figure this harness exists to produce would be wrong
            # in the engine's favour. It is counted and reported separately.
            if not (reply or "").strip():
                counts["no_reply"] += 1
                row["outcome"] = "no reply"
                rows.append(row)
                save()
                m = row.get("meta") or {}
                print(f"  {inst:5} {persona['id']:14} the model returned nothing"
                      f"  (done={m.get('done_reason')}, "
                      f"generated={m.get('eval_count')}, "
                      f"reasoning={m.get('thinking_chars')} chars)")
                continue

            produced = parse_plan(reply)
            if not produced:
                counts["unparseable"] += 1
                row["outcome"] = "unparseable"
                rows.append(row)
                save()
                print(f"  {inst:5} {persona['id']:14} unparseable")
                continue

            verdict = judge(engine, inst, key, produced, known, data, ref)
            row.update(verdict)
            bad = []
            if verdict["invented_codes"]:
                counts["invented"] += 1; bad.append(
                    f"{len(verdict['invented_codes'])} invented")
            if verdict["missing_units"] or verdict["total_credit_points"] != total_cp:
                counts["incomplete"] += 1; bad.append(
                    f"{len(verdict['missing_units'])} missing, "
                    f"{verdict['total_credit_points']}cp of {total_cp}")
            if verdict["violations"]:
                counts["invalid"] += 1; bad.append(
                    f"{verdict['violation_count']} violations")
            if not bad:
                counts["clean"] += 1
            row["outcome"] = "clean" if not bad else "; ".join(bad)
            rows.append(row)
            save()
            print(f"  {inst:5} {persona['id']:14} {row['outcome']}")

    print()
    print("=" * 66)
    print(f"plans requested                     : {counts['total']}")
    print(f"  the model returned nothing        : {counts['no_reply']}")
    print(f"  no schedule could be parsed       : {counts['unparseable']}")
    print(f"  used a unit code that is not real : {counts['invented']}")
    print(f"  incomplete or wrong credit total  : {counts['incomplete']}")
    print(f"  broke at least one published rule : {counts['invalid']}")
    print(f"  valid and complete                : {counts['clean']}")
    print()
    print("The engine's figure for the same question is 51 of 51 valid and "
          "complete (Section 4.2).")

    save()
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
