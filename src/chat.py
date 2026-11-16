"""
Run the planner conversationally against a local Ollama model.

    python3 chat.py "I failed Programming 1 in my UTS software degree"
    python3 chat.py                       # interactive
    python3 chat.py --trace "..."         # show every stage, not just the reply

Needs Ollama running locally with the model pulled:

    ollama pull qwen3:8b
    ollama serve

If Ollama cannot be reached this prints the engine's structured answer instead of
prose, rather than failing. The engine is the part that has to be right; the
model only puts it into words, so losing the model costs readability and nothing
else. That is the whole point of the division, and it is worth seeing it behave
that way rather than taking it on trust.
"""

import argparse
import json
import sys

import api
import llm_layer as L


def render_fallback(trace):
    """The structured answer, for when the model is unavailable or went wrong."""
    result = trace.get("result")
    if not result:
        return trace.get("reply", "No answer.")
    if not result.get("ok"):
        return f"The engine could not do that: {result.get('error')}"

    if "moved" in result:
        lines = [f"Failed unit: {result['failed_unit']}",
                 f"Repaired plan validates: {result['valid']}",
                 f"Units moved: {result['units_moved']}",
                 f"Finishes in: {result['finishes_in']}"]
        for m in result["moved"][:12]:
            lines.append(f"   {m['unit']}: {m['from']} -> {m['to']}")
        if result["units_moved"] > 12:
            lines.append(f"   ... and {result['units_moved'] - 12} more")
        return "\n".join(lines)

    if "periods" in result:
        lines = [result.get("title", "")]
        for p in result["periods"]:
            units = ", ".join(p["units"]) or "(open slots only)"
            lines.append(f"   {p['period']}: {units}  [{p['credit_points']}cp]")
        return "\n".join(lines)

    return json.dumps(result, indent=1)


def ask(engine, backend, message, show_trace=False):
    trace = L.answer(engine, backend, message)

    if show_trace:
        print("\n--- trace ---")
        print("stage    :", trace["stage"])
        print("profile  :", json.dumps(trace.get("profile"), indent=1))
        print("tool call:", json.dumps(trace.get("tool_call"), indent=1))
        if trace.get("tool_report", {}).get("errors"):
            print("tool errs:", trace["tool_report"]["errors"])
        if trace.get("intake", {}).get("errors"):
            print("intake   :", trace["intake"]["errors"],
                  "(repaired)" if trace["intake"]["repaired"] else "")
        print("grounding:", json.dumps(trace.get("grounding"), indent=1))
        print("--- end trace ---\n")

    print(trace["reply"])

    grounding = trace.get("grounding")
    if grounding and not grounding["grounded"]:
        # Reported rather than suppressed. A reply that cites something the
        # engine never produced is exactly the failure this layer exists to
        # prevent, so hiding it here would defeat the purpose.
        print("\n[ungrounded] This reply cites things the engine did not produce:")
        for field in ("unsupported_codes", "unsupported_numbers", "unsupported_contacts"):
            if grounding.get(field):
                print(f"   {field.replace('unsupported_', '')}: {grounding[field]}")
        print("\nThe engine's own answer:")
        print(render_fallback(trace))

    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", nargs="*", help="the question to ask")
    parser.add_argument("--trace", action="store_true",
                        help="show intake, tool call and grounding for each turn")
    parser.add_argument("--model", default=L.MODEL_NAME)
    parser.add_argument("--timeout", type=int, default=L.DEFAULT_TIMEOUT,
                        help="seconds to wait for each model call")
    parser.add_argument("--think", action="store_true",
                        help="let the model emit its reasoning block (much slower)")
    args = parser.parse_args()

    engine = api.Engine()
    loaded = {inst: len(plans) for inst, plans in engine.plans.items()}
    print(f"Engine loaded: {loaded}")

    backend = L.OllamaBackend(model=args.model, timeout=args.timeout,
                              no_think=not args.think)
    try:
        print("Waking the model (first call loads it into memory)...")
        backend.generate("reply with the single word ok", timeout=args.timeout)
    except Exception as e:
        print(f"\nOllama is not reachable ({type(e).__name__}). "
              f"Start it with 'ollama serve' and pull the model with "
              f"'ollama pull {args.model}'.\nThe engine still works; its structured "
              f"answers will be printed instead of prose.\n")
        backend = None

    if args.message:
        message = " ".join(args.message)
        if backend is None:
            print(json.dumps(api.list_plans(engine), indent=1)[:400])
            return
        ask(engine, backend, message, args.trace)
        return

    if backend is None:
        print("Interactive mode needs the model. Exiting.")
        return

    print("Ask a question, or Ctrl-D to stop.\n")
    while True:
        try:
            message = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if message:
            ask(engine, backend, message, args.trace)
            print()


if __name__ == "__main__":
    main()