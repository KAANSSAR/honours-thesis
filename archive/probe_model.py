"""
Why did the model return an empty reply?

Not part of the evaluation. This asks Ollama three questions and prints what
comes back before any of our own processing touches it, so that an empty reply
can be attributed rather than guessed at. The three cases separate the
possibilities:

  1 trivial prompt            is the server answering at all?
  2 the real baseline prompt  does the length of the prompt or the difficulty
                              of the task change the answer?
  3 the same, no /no_think    is the thinking suppressor the thing breaking it?

What matters in the output is `done_reason` and `eval_count`. `length` means the
reply was cut off by the token cap. `stop` with a low eval_count means the model
chose to say nothing. An empty `response` with either is the thing to explain.

Usage:
    python3 probe_model.py
"""

import json
import re
import sys

import api
import eval_baseline as B
import llm_layer as L
import rules as R

URL = L.OLLAMA_URL

# The same strip the backend applies, repeated here rather than called, so the
# probe reports what the model sent as well as what the backend would keep.
STRIP = lambda t: re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()


def ask(label, prompt, num_predict, no_think, think_param=None):
    import requests
    body = {"model": L.MODEL_NAME,
            "prompt": prompt + ("\n" + L.NO_THINK if no_think else ""),
            "stream": False,
            "options": {"temperature": 0.0}}
    if num_predict:
        body["options"]["num_predict"] = num_predict
    if think_param is not None:
        body["think"] = think_param

    print("=" * 70)
    print(f"{label}")
    print(f"  prompt characters: {len(body['prompt'])}   "
          f"num_predict: {num_predict or 'unset'}   /no_think: {no_think}   "
          f"think param: {think_param}")
    try:
        r = requests.post(URL, json=body, timeout=600)
    except Exception as exc:
        print(f"  FAILED before a reply: {type(exc).__name__}: {exc}")
        return
    if r.status_code != 200:
        print(f"  http status      : {r.status_code}")
        print(f"  body             : {r.text[:300]}")
        if r.status_code == 400 and think_param is not None:
            print("  -> this Ollama build does not accept the 'think' "
                  "parameter.")
        return
    data = r.json()

    raw = data.get("response", "")
    stripped = STRIP(raw)
    print(f"  done_reason      : {data.get('done_reason')}")
    print(f"  prompt_eval_count: {data.get('prompt_eval_count')}")
    print(f"  eval_count       : {data.get('eval_count')}")
    print(f"  http status      : {r.status_code}")
    print(f"  thinking field   : {len(data.get('thinking') or '')} chars")
    print(f"  raw length       : {len(raw)}")
    print(f"  after <think> strip: {len(stripped)}")
    print(f"  contains '<think>' : {'<think>' in raw}")
    print(f"  contains '</think>': {'</think>' in raw}")
    print("  --- first 900 characters of the RAW reply ---")
    print(raw[:900] if raw else "  (the reply was empty)")
    print()


def main():
    engine = api.Engine(".")
    inst, key = "UTS", "Software Engineering major"
    units, codes, data, ref = B.unit_lines(engine, inst, key)
    rules = engine.rules[inst]
    total_cp = sum(rules.unit_cp(c, data) for v in ref.values() for c in v)
    teaching = [i for i in sorted(ref) if i % R.POSITIONS_PER_YEAR in (0, 2)]
    real = B.PROMPT.format(degree=f"{inst} {key}", total=total_cp,
                           periods=len(teaching),
                           max_cp=rules.max_cp_per_period, units=units,
                           message="Give me a full plan for this degree.")

    ask("1. trivial prompt, /no_think on",
        'Reply with this exact JSON and nothing else: {"ok": true}',
        256, True)

    ask("2. the real baseline prompt, as the harness sends it",
        real, 2048, True)

    ask("3. the real baseline prompt, reasoning suppressed at the API",
        real, 2048, True, think_param=False)

    print("=" * 70)
    print("Case 3 is the fix. If its raw length is non-zero and done_reason "
              "is 'stop',\nthe harness will work. If case 3 returns HTTP 400, "
          "this Ollama build does not\nknow the 'think' parameter and the "
          "answer is a different one.")


if __name__ == "__main__":
    main()
