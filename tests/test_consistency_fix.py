"""
Section 4.5.5: does the fix remove the divergence the measurement found?

`eval_consistency.py` needs a running model and takes over an hour. This does
not. It replays the profiles the model actually produced during that run,
recorded per persona in consistency_results.json, through the deterministic
stages as they now stand, and counts how many distinct answers the seven
personas of each scenario arrive at.

That is a fair test of this particular fix and not of the model, which is the
point. The divergence entered at intake, and every stage after intake is
deterministic code; so replaying the same intake output through the repaired
code answers exactly the question the fix claims to settle, without giving the
model a second chance to behave differently and without crediting the fix for
a lucky re-roll.

What it does not test is whether the model's intake output itself improves, which
is what the second_plan_mention field is for. Only a full re-run measures that,
and the figures in Section 4.5.3 remain the measured ones.

Three things are compared per persona, being the three stages the measurement
found moving:

  intent      which kind of request this is
  plan_key    which enrolment plan it is about
  path        whether the request is answered or sent back as a question

Usage:
    python3 test_consistency_fix.py
    python3 test_consistency_fix.py --results consistency_results.json
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import json
import sys

import api
import llm_layer as L
from paths import P

# Fields intake fills. The recorded profile is the resolved one, so a plan key
# in it may already have been resolved and a unit name may already be a code;
# both resolve to themselves on a second pass, so replaying them is sound.
INTAKE_FIELDS = ("institution", "plan_key", "plan_mention", "second_plan_mention",
                 "failed_units", "completed_units", "intent", "unit_of_interest")


def as_intake(recorded):
    """The recorded profile, read back as though it had just left intake."""
    if not recorded:
        return None
    out = {}
    for field in INTAKE_FIELDS:
        value = recorded.get(field)
        if field in ("failed_units", "completed_units"):
            out[field] = list(value or [])
        else:
            out[field] = value
    out["intent"] = out.get("intent") or "unclear"
    return out


def settled(engine, profile):
    """
    Run the deterministic stages and report the answer the student would get.

    What is compared is the answer, not the internal fields that produced it. A
    request that cannot be resolved is answered with a question, and two students
    asked the same question have had the same answer even if intake filed the
    degree under `plan_key` for one of them and `plan_mention` for the other.
    Comparing the fields instead would count that filing difference as a
    difference in treatment, which it is not; the measured run, which compared
    the engine's result, counted it as one answer, and this has to agree with the
    measurement to be comparable to it. `check_against_measured` enforces that
    agreement rather than leaving it to be taken on trust.
    """
    profile, override = L.settle_intent(dict(profile))
    resolved, resolution = L.resolve_profile(engine, profile)

    if resolved["intent"] == "compare_degrees":
        pair = L.resolve_comparison(engine, resolved)
        if pair["ok"]:
            return {"answer": f"compare {pair['usyd_key']} | {pair['uts_key']}",
                    "path": "answered", "override": override}
        return {"answer": f"question: {pair['error']}",
                "path": "needs_clarification", "override": override}

    needs_plan = resolved["intent"] in {"get_plan", "validate_plan",
                                        "replan_after_failure"}
    if (needs_plan and not resolved.get("plan_key")) or resolution["ambiguous"]:
        return {"answer": f"question: {L.describe_gaps(resolution)}",
                "path": "needs_clarification", "override": override}
    if resolved["intent"] == "replan_after_failure" and not resolved["failed_units"]:
        return {"answer": "question: which unit did you fail",
                "path": "needs_clarification", "override": override}

    return {"answer": f"{resolved['intent']} {resolved.get('plan_key')} "
                      f"{sorted(resolved['failed_units'])}",
            "path": "answered", "override": override}


def check_against_measured(measured):
    """
    The measured run's own replies, grouped, so 'before' is not taken on trust.

    `distinct_results` alone counts the engine's structured answer, which is null
    for every persona of a request that ended in a question; a scenario where
    some personas were answered and others were asked something would therefore
    read as more consistent than it was. So the comparison is the engine's result
    where there is one and the question where there is not.

    The prose is not compared, for the reason Section 4.5 gives: two wordings of
    the same plan are not an inconsistency, and counting them as one would report
    the model's vocabulary as bias. A question is compared as text because the
    question *is* the answer in that case.
    """
    out = {}
    for scenario, rows in (measured.get("rows") or {}).items():
        groups = {}
        for row in rows:
            if row.get("error"):
                continue
            result = row.get("result")
            key = (json.dumps(result, sort_keys=True, default=str) if result
                   else f"question: {row.get('reply')}")
            groups.setdefault(key, []).append(row["persona"])
        out[scenario] = len(groups)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=P("consistency_results.json"))
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every persona, not only the scenarios that moved")
    args = ap.parse_args()

    try:
        with open(args.results, encoding="utf-8") as fh:
            measured = json.load(fh)
    except FileNotFoundError:
        sys.exit(f"{args.results} not found. Run eval_consistency.py first, or "
                 f"pass --results with the path to a recorded run.")

    engine = api.Engine()
    rows = measured.get("rows") or {}
    if not rows:
        sys.exit("that results file has no per-persona rows to replay")
    before_counts = check_against_measured(measured)

    print("=" * 76)
    print("Replaying the recorded intake through the repaired deterministic stages")
    print("=" * 76)
    print(f"{'scenario':34} {'before':>8} {'after':>8} {'overrides':>10}  note")
    print("-" * 76)

    moved_before, moved_after, overrides_total, outstanding = 0, 0, 0, []
    detail = {}

    for scenario, persona_rows in rows.items():
        before_distinct = before_counts.get(scenario, 0)
        answers, overrides, skipped = {}, 0, 0
        for row in persona_rows:
            profile = as_intake(row.get("profile"))
            if profile is None:                 # a transport failure, not a reading
                skipped += 1
                continue
            landing = settled(engine, profile)
            if landing["override"]:
                overrides += 1
            answers.setdefault(landing["answer"], []).append(row["persona"])

        after_distinct = len(answers)
        overrides_total += overrides
        detail[scenario] = {"before": before_distinct, "after": after_distinct,
                            "overrides": overrides, "skipped": skipped,
                            "groups": answers}

        if before_distinct > 1:
            moved_before += 1
        note = ""
        if after_distinct > 1:
            moved_after += 1
            outstanding.append(scenario)
            note = "still varies"
        elif before_distinct > 1:
            note = "fixed"
        print(f"{scenario:34} {before_distinct:>8} {after_distinct:>8} "
              f"{overrides:>10}  {note}")
        if args.verbose or after_distinct > 1:
            for key, personas in answers.items():
                print(f"      {key[:96]}")
                print(f"        {', '.join(personas)}")
        if skipped:
            print(f"      {skipped} persona(s) had no profile to replay: the "
                  f"model timed out during the measured run")

    total = len(rows)
    print("-" * 76)
    print(f"scenarios whose answer varied by persona, as measured  : "
          f"{moved_before} of {total}")
    print(f"scenarios whose answer varies by persona, after the fix: "
          f"{moved_after} of {total}")
    print(f"intents the rule had to correct: {overrides_total}")
    if outstanding:
        print()
        print("Still varying, and why the replay cannot settle them:")
        for scenario in outstanding:
            print(f"  {scenario}: the divergence is in what intake extracted, not "
                  f"in what\n    the code did with it. The repaired intake schema "
                  f"is what addresses that,\n    and only a re-run with the model "
                  f"measures whether it does.")

    json.dump(detail, open(P("consistency_fix_replay.json"), "w"), indent=1)
    print("\nwrote " + P("consistency_fix_replay.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
