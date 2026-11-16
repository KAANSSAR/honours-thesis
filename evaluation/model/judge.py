"""
Is the explanation faithful to the engine's answer?

Section 4.5.4 reports that 40 of 41 explanations quoted nothing the engine had
not produced. That is the grounding check of Section 3.9, and its own docstring
states the limit: it compares codes and figures and cannot catch a reply that
quotes the right ones and describes them wrongly. This asks the question the
check cannot.

The judge is given the engine's structured result as ground truth and asked only
whether the prose asserts anything that result does not support. It is not asked
whether the plan is correct: that is the validator's question and it has a
deterministic answer. Three verdicts:

  faithful       every claim is supported by the result
  unsupported    a claim the result neither states nor contradicts, including
                 arithmetic the model did itself
  contradicted   a claim the result contradicts

The reference verdicts below were produced by a model outside the Qwen family,
because a judge that is the same 8B marks its own work. They are recorded rather
than recomputed so the figure in the thesis is reproducible without a model, and
`--backend ollama` re-runs the judgement against a local model for comparison.

Usage:
    python3 judge.py <repo dir>                  # report the reference verdicts
    python3 judge.py <repo dir> --backend ollama --model <name>
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from paths import P

import argparse
import collections
import json
import os
import sys

PROMPT = """You are checking whether a written answer is faithful to a structured result.

The result is ground truth. Do not judge whether the result itself is correct.
Judge only whether the answer asserts anything the result does not support.

Reply with ONE JSON object and nothing else:
  {{"verdict": "faithful" | "unsupported" | "contradicted", "why": "<one sentence>"}}

  faithful      every claim in the answer is supported by the result
  unsupported   a claim the result neither states nor contradicts, including a
                number the model computed from the result's own figures
  contradicted  a claim the result contradicts

Engine result:
{result}

The answer given to the student:
{reply}

JSON:"""

# n -> (verdict, why). Every exchange the measured run recorded with both a
# reply and an engine result.
REFERENCE = {
    1: ("unsupported", "says the failure leaves the path unaffected; the engine validated the original plan and made no finding about the failure"),
    2: ("contradicted", "says there is no need to repeat the failed course, where the result reschedules a retake of 41039"),
    3: ("faithful", ""),
    4: ("faithful", ""),
    5: ("faithful", ""),
    6: ("faithful", ""),
    7: ("faithful", ""),
    8: ("contradicted", "says no plan was found and refers the student to an advisor, where the result is a valid plan"),
    9: ("faithful", ""),
    10: ("faithful", ""),
    11: ("faithful", ""),
    12: ("faithful", ""),
    13: ("contradicted", "says no plan was found, where the result is a valid plan, and restates the student's stated ambition as a constraint the engine considered"),
    14: ("faithful", ""),
    15: ("faithful", ""),
    16: ("unsupported", "reports that the result does not mention a plan for TAFE transfers, which imports the student's own description into the answer"),
    17: ("faithful", ""),
    18: ("faithful", ""),
    19: ("faithful", ""),
    20: ("unsupported", "reports the absence of an accelerated plan, which imports the student's stated goal into the answer"),
    21: ("contradicted", "gives Sydney the higher total credit points, where the result has Sydney at 192 and UTS at 198"),
    22: ("faithful", ""),
    23: ("contradicted", "gives Sydney more units with requisites, where the result has Sydney at 14 and UTS at 19"),
    24: ("contradicted", "gives Sydney more units with requisites, where the result has Sydney at 14 and UTS at 19"),
    25: ("faithful", ""),
    26: ("faithful", ""),
    27: ("unsupported", "reports 33 UTS units, which is the result's 29 placed plus 4 open slots added by the model"),
    28: ("faithful", ""),
    29: ("faithful", ""),
    30: ("faithful", ""),
    31: ("faithful", ""),
    32: ("faithful", ""),
    33: ("faithful", ""),
    34: ("faithful", ""),
    35: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    36: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    37: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    38: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    39: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    40: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
    41: ("contradicted", "gives AMME2000 and AMME2500 as alternatives, where the result requires both"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=P("consistency_results.json"),
                    help="a recorded run of eval_consistency.py")
    ap.add_argument("--backend", choices=("reference", "ollama"),
                    default="reference")
    ap.add_argument("--model", default="qwen3:8b")
    args = ap.parse_args()

    import llm_layer as L

    run = json.load(open(args.input, encoding="utf-8"))
    rows = []
    for scenario, entries in run["rows"].items():
        for entry in entries:
            if entry.get("reply") and entry.get("result"):
                rows.append({"scenario": scenario, "persona": entry["persona"],
                             "reply": entry["reply"], "result": entry["result"]})
    for i, row in enumerate(rows, 1):
        row["n"] = i

    if args.backend == "ollama":
        backend = L.OllamaBackend(model=args.model)
        for row in rows:
            raw = backend.generate(PROMPT.format(
                result=json.dumps(row["result"])[:3000], reply=row["reply"]))
            try:
                start, end = raw.find("{"), raw.rfind("}")
                obj = json.loads(raw[start:end + 1])
                row["verdict"] = obj.get("verdict", "unparseable")
                row["why"] = obj.get("why", "")
            except Exception:
                row["verdict"], row["why"] = "unparseable", raw[:120]
    else:
        for row in rows:
            row["verdict"], row["why"] = REFERENCE.get(
                row["n"], ("missing", "no reference verdict recorded"))

    # The grounding check, recomputed here so the two measures are compared on
    # the same exchanges rather than on two different runs.
    for row in rows:
        row["grounded"] = L.check_grounding(row["reply"], row["result"])["grounded"]

    counts = collections.Counter(r["verdict"] for r in rows)
    print(f"exchanges judged: {len(rows)}  (backend: {args.backend})")
    for verdict in ("faithful", "unsupported", "contradicted", "unparseable",
                    "missing"):
        if counts.get(verdict):
            print(f"  {verdict:14} {counts[verdict]}")

    unfaithful = [r for r in rows if r["verdict"] in ("unsupported",
                                                      "contradicted")]
    flagged = [r for r in rows if not r["grounded"]]
    caught = [r for r in unfaithful if not r["grounded"]]
    print(f"\ngrounding check flagged  : {len(flagged)} of {len(rows)}")
    print(f"the judge found unfaithful: {len(unfaithful)} of {len(rows)}")
    print(f"found by both             : {len(caught)}")
    print(f"found only by the judge   : {len(unfaithful) - len(caught)}")
    print(f"flagged but judged faithful: {len(flagged) - len(caught)}")

    print("\nwhat the mechanical check could not see:")
    for row in unfaithful:
        if row["grounded"]:
            print(f"  [{row['n']:2}] {row['verdict']:13} {row['scenario']}"
                  f" / {row['persona']}")
            print(f"       {row['why']}")

    by_cause = collections.Counter(r["why"].split(";")[0][:52]
                                   for r in unfaithful)
    print("\nby cause:")
    for why, count in by_cause.most_common():
        print(f"  {count:2}  {why}")

    json.dump(rows, open(P("judge_results.json"), "w"), indent=1)
    print("\nwrote " + P("judge_results.json"))


if __name__ == "__main__":
    main()
