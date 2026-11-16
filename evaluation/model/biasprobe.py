"""
Can anything a student says about themselves reach the engine's input?

Section 3.8 undertakes that it cannot. The engine side of that is a proof rather
than a measurement, because the persona is not an argument to any engine
function, and `eval_consistency.py --backend engine-only` reports it. This probes
the part of the claim that is not a proof: the language layer, which does read
the message.

Two things are checked, neither needing a running model.

1. Persona blindness of the deterministic stages. The same intake JSON is run
   through the whole layer once per persona prefix. Everything after intake
   reads extracted fields rather than the message, with one exception, so the
   resolved profile and the engine call must be identical for every persona.

2. The exception. `repair_institution` fills the institution field from the
   message when intake left it empty, which is a deliberate reach into the text
   and therefore the one place a self-description could select a plan. It is
   probed with biographical phrasings that mention a place without naming where
   the student studies.

Usage:
    python3 biasprobe.py <repo dir>
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from paths import P

import argparse
import json
import os
import sys

passed = failed = 0
NOTES = []


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {detail}")
        NOTES.append((name, detail))


# Phrasings that name a place without saying where the student is enrolled. A
# curriculum plan must not be selected by any of them.
BIOGRAPHICAL = [
    "I moved to Sydney last year. ",
    "I grew up in Sydney but my family is overseas. ",
    "I commute in from western Sydney every day. ",
    "I did my HSC in Sydney. ",
    "My brother went to UTS and said it was good. ",
    "I was deciding between UTS and somewhere else before I enrolled. ",
]


def main():
    ap = argparse.ArgumentParser()
    ap.parse_args()
    import api
    import llm_layer as L

    engine = api.Engine()
    profiles = json.load(open(P("profiles.json"), encoding="utf-8"))
    personas = profiles["personas"]

    print(f"\npersona blindness of the deterministic stages "
          f"({len(personas)} personas)")

    # One intake reading, reused for every persona. If the layer is blind to the
    # message after intake, the persona cannot change anything downstream.
    intake = json.dumps({"institution": "UTS", "plan_key": None,
                         "plan_mention": "Software Engineering major",
                         "second_plan_mention": None,
                         "failed_units": ["Programming 1"],
                         "completed_units": [], "intent": "replan_after_failure",
                         "unit_of_interest": None})
    request = "I failed Programming 1, what happens to my plan?"

    landings = {}
    for persona in personas:
        trace = L.answer(engine, L.StubBackend([intake, "prose"]),
                         persona["prefix"] + request)
        landings[persona["id"]] = json.dumps(
            {"call": trace.get("tool_call"),
             "stage": trace.get("stage"),
             "moved": (trace.get("result") or {}).get("units_moved")},
            sort_keys=True)
    distinct = set(landings.values())
    check("every persona reaches one engine call", len(distinct) == 1,
          f"{len(distinct)} distinct: "
          f"{[k for k, v in landings.items() if v != landings['plain']][:4]}")

    by_group = {}
    for persona in personas:
        by_group.setdefault(persona.get("group", "?"), set()).add(
            landings[persona["id"]])
    for group, vals in sorted(by_group.items()):
        check(f"  group '{group}' is internally identical", len(vals) == 1)

    blank = {"institution": None, "plan_key": None, "plan_mention": None,
             "second_plan_mention": None, "failed_units": [],
             "completed_units": [], "intent": "unclear",
             "unit_of_interest": None}
    SYD = ("Software_stream_Computer_Engineering_breadth_"
           "Humanitarian_Engineering")
    pair_context = {**blank, "plan_mention": "Software at Sydney",
                    "second_plan_mention": "the same major at UTS",
                    "intent": "compare_degrees",
                    "compared_usyd_key": SYD,
                    "compared_uts_key": "Software Engineering major"}

    print("\nwhen the repair is allowed to run at all")
    first = L.answer(engine, L.StubBackend([json.dumps(blank), "prose"]),
                     "I moved to Sydney last year, show me a plan")
    check("a first request never has its university read out of the message",
          first.get("institution_repaired") is None,
          f"read {first.get('institution_repaired')}")

    after = L.answer(engine, L.StubBackend([json.dumps(blank), "prose"]),
                     "now just show me the Sydney one", context=dict(pair_context))
    check("a follow-up to a comparison does",
          after.get("institution_repaired") == "USYD",
          f"read {after.get('institution_repaired')}")
    check("and it lands on that side's plan rather than a comparison",
          (after.get("tool_call") or {}).get("tool") == "get_plan"
          and after["tool_call"]["args"]["key"] == SYD,
          str(after.get("tool_call")))

    for message, want in (("now just show me the Sydney one", "USYD"),
                          ("and the UTS one?", "UTS")):
        _, named = L.repair_institution(dict(blank), message)
        check(f"{message!r} resolves to {want}", named == want, f"got {named}")

    # A persona must not be able to flip it even where it is allowed to run.
    print("\nthe repair, probed with every persona")
    for persona in personas:
        _, named = L.repair_institution(dict(blank), persona["prefix"] + request)
        check(f"persona {persona['id']} selects nothing", named is None,
              f"set institution to {named}")

    # What follows is reported rather than asserted, because it is the function's
    # known reach and not a contract it breaks. The protection is the gate above:
    # the function matches a university name wherever it appears, and only a
    # follow-up to a comparison is ever allowed to call it. Inside such a
    # follow-up it cannot tell a reference from biography, and that residual is
    # the honest statement of what this repair costs.
    print("\nresidual reach, reported not asserted")
    reached = []
    for prefix in BIOGRAPHICAL:
        _, named = L.repair_institution(dict(blank), prefix + request)
        if named:
            reached.append((prefix.strip(), named))
    for text, named in reached:
        print(f"  note  {text[:52]!r} would read {named}")
    print(f"  {len(reached)} of {len(BIOGRAPHICAL)} biographical phrasings name a "
          f"university the student did not say they study at.")

    biographical_after = L.answer(
        engine, L.StubBackend([json.dumps(blank), "prose"]),
        "I moved to Sydney last year, remind me what we were comparing",
        context=dict(pair_context))
    verdict = biographical_after.get("institution_repaired")
    print(f"  note  inside a comparison follow-up, that phrasing reads "
          f"{verdict} and answers about one side rather than the pair.")
    print("  This is the limitation the gate narrows but does not remove.")

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
