"""
Section 4.5: does the same request answered for different students get the same
answer?

Each scenario in profiles.json is asked once per persona. The personas differ
only in background, entry pathway and goals, which Section 3.8 records and
excludes from the comparison, so every persona of a scenario must produce the
same structured answer. A difference can only have entered through the language
model's reading of the request, which is what makes this a sharp test of the
architecture rather than of the engine.

Four things are measured separately, because a single pass or fail would hide
which stage moved:

  intake      the resolved profile: degree, plan, units, intent
  tool call   which engine function was chosen, and with what arguments
  result      the engine's structured answer, compared exactly
  explanation the prose, compared on the facts it carries rather than its words

Only the first two and the last involve the model. The engine is never shown the
persona, so a difference in the third would mean the model passed something
different into it.

The explanation is not compared verbatim. Two wordings of the same plan are not
an inconsistency, and demanding identical prose would report the model's
vocabulary as bias. What must not differ is the content: the unit codes named,
the numbers quoted, and whether a recommendation was made at all. Those are
extracted and compared as sets.

Usage:
    python3 eval_consistency.py --backend ollama          # the real run
    python3 eval_consistency.py --backend engine-only     # no model needed
    python3 eval_consistency.py --backend ollama --only compare-software,compare-civil
    python3 eval_consistency.py --backend ollama --model qwen3:8b --out results.json

Results are written after every scenario rather than only at the end, so a run
that dies on its last scenario does not cost the ones before it. The first run
of this harness wrote nothing until all eight had finished, which was a poor
design for an hour-long job on one laptop.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import argparse
import collections
import json
import os
import re
import sys

import api
import llm_layer as L
from paths import P

NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
CODE = re.compile(r"\b[A-Z]{2,5}\d{3,4}\b|\b\d{5}\b")


def load_profiles(path=P("profiles.json")):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def facts_in(text):
    """The checkable content of a reply: the codes it names and the figures it quotes."""
    if not text:
        return {"codes": set(), "numbers": set()}
    return {"codes": set(CODE.findall(text)),
            # Single digits are prose ("one unit", "year 2") rather than claims.
            "numbers": {n for n in NUMBER.findall(text) if len(n) >= 2}}


def canonical(obj):
    """A stable string for comparing two structured answers exactly."""
    return json.dumps(obj, sort_keys=True, default=str)


def run_scenario(engine, backend_factory, scenario, personas):
    """Ask one scenario once per persona. Returns a row per persona."""
    rows = []
    for persona in personas:
        message = (persona["prefix"] + scenario["request"]).strip()
        backend = backend_factory()
        try:
            trace = L.answer(engine, backend, message)
            error = None
        except Exception as exc:                      # a model or transport failure
            trace, error = {}, f"{type(exc).__name__}: {exc}"
        rows.append({
            "persona": persona["id"],
            "message": message,
            "error": error,
            "stage": trace.get("stage"),
            "intent": (trace.get("resolved") or trace.get("profile") or {}).get("intent"),
            "profile": trace.get("resolved") or trace.get("profile"),
            "tool_call": trace.get("tool_call"),
            "result": trace.get("result"),
            "reply": trace.get("reply"),
            # The grounding verdict was computed on every exchange and then
            # discarded by this harness, so the first run could not report how
            # often the explanation quoted something the engine had not produced.
            # It is the check Section 3.9 rests on and it belongs in the record.
            "grounding": trace.get("grounding"),
            "intent_override": trace.get("intent_override"),
        })
    return rows


def compare(rows):
    """How many distinct answers did the personas produce at each stage?"""
    def distinct(key, transform=canonical):
        seen = {}
        for r in rows:
            if r["error"]:
                continue
            seen.setdefault(transform(r[key]), []).append(r["persona"])
        return seen

    profiles = distinct("profile")
    calls = distinct("tool_call")
    results = distinct("result")

    facts = {}
    for r in rows:
        if r["error"]:
            continue
        f = facts_in(r["reply"])
        facts.setdefault(
            (frozenset(f["codes"]), frozenset(f["numbers"])), []).append(r["persona"])

    # An explanation that quotes a code or figure the engine did not produce,
    # counted here rather than left in the traces. A derived number is the case
    # to watch: the engine's own figures added together is not a fact the engine
    # stated, and the check is strict about that on purpose.
    judged = [r for r in rows if not r["error"] and (r.get("grounding") or {})]
    ungrounded = [r["persona"] for r in judged
                  if not (r["grounding"] or {}).get("grounded", True)]

    return {
        "personas_run": sum(1 for r in rows if not r["error"]),
        "errors": [r["persona"] for r in rows if r["error"]],
        "replies_judged": len(judged),
        "ungrounded": ungrounded,
        "overrides": [r["persona"] for r in rows if r.get("intent_override")],
        "distinct_profiles": len(profiles),
        "distinct_tool_calls": len(calls),
        "distinct_results": len(results),
        "distinct_explanation_facts": len(facts),
        "profile_groups": {k[:120]: v for k, v in profiles.items()} if len(profiles) > 1 else {},
        "result_groups": {k[:120]: v for k, v in results.items()} if len(results) > 1 else {},
        "fact_groups": ({str(sorted(k[0]))[:90]: v for k, v in facts.items()}
                        if len(facts) > 1 else {}),
    }


class EngineOnly:
    """
    A backend that fails on first use.

    Used to measure the half of the question that needs no model: the engine is
    never shown a persona, so its answer to a fixed profile cannot vary by one.
    That half is proved directly in `engine_determinism` rather than sampled.
    """

    def generate(self, prompt, timeout=None):
        raise RuntimeError("engine-only mode makes no model calls")


def engine_determinism(engine, profiles):
    """
    Feed the engine the same resolved profile once per persona and compare.

    This is the part of Section 4.5 that does not depend on the model being
    available, and it is a proof rather than a sample: the persona is not an
    argument to any engine function, so the answers are identical unless the
    engine carries state between calls.
    """
    out = []
    for scenario in profiles["scenarios"]:
        intent = scenario["expect_intent"]
        answers = []
        for persona in profiles["personas"]:
            if intent == "compare_degrees":
                pairs = __import__("pathA").pairs(engine)
                key = "Software" if "software" in scenario["id"] else "Civil"
                pair = next((p for p in pairs if key.lower() in p[0].lower()), pairs[0])
                result = __import__("pathA").compare(engine, pair[0], pair[1])
            elif intent == "lookup_unit":
                result = api.lookup_unit(engine, "USYD", "AMME3500")
            else:
                inst = "UTS" if "uts" in scenario["id"] else "USYD"
                plan = sorted(engine.plans[inst])[0]
                result = api.get_plan(engine, inst, plan)
            answers.append(canonical(result))
        out.append({"scenario": scenario["id"],
                    "personas": len(answers),
                    "distinct_answers": len(set(answers))})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("ollama", "engine-only"), default="ollama")
    ap.add_argument("--model", default=L.MODEL_NAME)
    ap.add_argument("--timeout", type=int, default=L.DEFAULT_TIMEOUT)
    ap.add_argument("--profiles", default=P("profiles.json"))
    ap.add_argument("--out", default=P("consistency_results.json"))
    ap.add_argument("--only", default=None,
                    help="comma-separated scenario ids to run, instead of all of "
                         "them. Part 1 always covers every scenario, being free.")
    ap.add_argument("--merge", action="store_true",
                    help="keep the scenarios already in --out that this run does "
                         "not cover, instead of replacing the file")
    args = ap.parse_args()

    engine = api.Engine()
    profiles = load_profiles(args.profiles)

    print("=" * 72)
    print("PART 1 - the engine's own determinism, no model involved")
    print("=" * 72)
    det = engine_determinism(engine, profiles)
    bad = [d for d in det if d["distinct_answers"] != 1]
    for d in det:
        mark = "ok  " if d["distinct_answers"] == 1 else "DIFF"
        print(f"  {mark}  {d['scenario']:34} {d['personas']} personas, "
              f"{d['distinct_answers']} distinct answer(s)")
    print(f"\nscenarios where the engine's answer varied by persona: {len(bad)} "
          f"of {len(det)}")

    if args.backend == "engine-only":
        json.dump({"engine_determinism": det}, open(args.out, "w"), indent=1)
        print(f"\nwrote {args.out}")
        print("Model-side measurement skipped. Re-run with --backend ollama "
              "on a machine with the model running.")
        return

    def factory():
        return L.OllamaBackend(model=args.model, timeout=args.timeout)

    print()
    print("=" * 72)
    print(f"PART 2 - the whole system, {args.model} at temperature "
          f"{L.TEMPERATURE}")
    print("=" * 72)
    rows, summary = {}, {}
    if args.merge and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            prior = json.load(fh)
        rows.update(prior.get("rows") or {})
        summary.update(prior.get("summary") or {})
        print(f"\ncarrying {len(rows)} scenario(s) forward from {args.out}")

    wanted = ([s.strip() for s in args.only.split(",") if s.strip()]
              if args.only else None)
    chosen = [s for s in profiles["scenarios"]
              if wanted is None or s["id"] in wanted]
    if wanted:
        missing = sorted(set(wanted) - {s["id"] for s in chosen})
        if missing:
            sys.exit(f"no such scenario(s): {', '.join(missing)}")
        print(f"\nrunning {len(chosen)} of {len(profiles['scenarios'])} "
              f"scenarios: {', '.join(s['id'] for s in chosen)}")

    def save():
        json.dump({"engine_determinism": det, "summary": summary, "rows": rows},
                  open(args.out, "w"), indent=1, default=str)

    for scenario in chosen:
        print(f"\n{scenario['id']}")
        got = run_scenario(engine, factory, scenario, profiles["personas"])
        rows[scenario["id"]] = got
        s = compare(got)
        summary[scenario["id"]] = s
        # Written now, not at the end: an hour of results should not depend on
        # the last scenario surviving.
        save()
        print(f"   personas answered      : {s['personas_run']} "
              f"of {len(profiles['personas'])}")
        if s["errors"]:
            print(f"   errors                 : {s['errors']}")
        for label, key in (("distinct intake profiles", "distinct_profiles"),
                           ("distinct tool calls     ", "distinct_tool_calls"),
                           ("distinct engine results ", "distinct_results"),
                           ("distinct explanation facts", "distinct_explanation_facts")):
            flag = "" if s[key] <= 1 else "   <-- varies by persona"
            print(f"   {label}: {s[key]}{flag}")
        if s.get("overrides"):
            print(f"   intent corrected by rule: {', '.join(s['overrides'])}")
        if s.get("ungrounded"):
            print(f"   ungrounded explanation : {', '.join(s['ungrounded'])}"
                  f"   <-- quoted something the engine did not produce")

    total = len(summary)
    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    for label, key in (("same intake across personas", "distinct_profiles"),
                       ("same tool call", "distinct_tool_calls"),
                       ("same engine result", "distinct_results"),
                       ("same facts in the explanation", "distinct_explanation_facts")):
        n = sum(1 for s in summary.values() if s[key] == 1)
        print(f"  {label:34} {n} of {total} scenarios")

    judged = sum(s.get("replies_judged", 0) for s in summary.values())
    ungrounded = sum(len(s.get("ungrounded") or ()) for s in summary.values())
    corrected = sum(len(s.get("overrides") or ()) for s in summary.values())
    print()
    print(f"  {'explanations judged for grounding':34} {judged}")
    print(f"  {'of those, ungrounded':34} {ungrounded}")
    print(f"  {'intents corrected by rule':34} {corrected}")

    save()
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
