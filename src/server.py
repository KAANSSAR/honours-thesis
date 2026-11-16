"""
The web frontend's server. Section 3.10.

The engine's functions are reached here exactly as the language model reaches
them, through `api`, which is the point of that section: the frontend and the
model call the same functions, so a student can use the plan view without the
conversation and neither path can drift from the other. Nothing in this file
decides whether a placement is legal. It serves JSON from `api`, `pathA` and
`llm_layer` and static files, and that is all.

Two deliberate constraints:

  No dependencies. Python's own http.server, so the frontend runs with no
  install step on any machine that can already run the engine. A thesis
  artefact that needs a package manager to demonstrate is one that stops
  demonstrating the moment the package manager changes.

  No state. Every request is answered from the dataset. There is no session, no
  stored student record and no accumulated conversation, so a reload cannot
  produce a different answer from a fresh visit, which is the same property
  Section 4.5.2 proves of the engine itself.

The conversation endpoint needs the local model and the rest does not. When
Ollama is absent the endpoint says so and the other three views keep working,
because the plan, the comparison and the repair are the engine's answers and
have no reason to depend on a model being up.

Usage:
    python3 server.py                 # then open http://localhost:8000
    python3 server.py --port 9000
    python3 server.py --no-model      # skip the model entirely
"""

import argparse
import json
import mimetypes
import os
import posixpath
import re
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import api
import pathA
import rules as R

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# One engine for the process. Building it reads the whole dataset, and the
# engine holds no per-request state, so sharing it is safe and avoids paying
# that cost on every click. The lock guards the lazily built caches inside it.
_ENGINE = None
_LOCK = threading.Lock()


def engine(root=None):
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            _ENGINE = api.Engine(root)
        return _ENGINE


# ---------------------------------------------------------------- plan view

def _unit_names(eng, institution):
    """code -> name, for whichever institution's units are in scope."""
    out = {}
    for code, unit in (api._units_for(eng, institution) or {}).items():
        name = unit.get("name") or unit.get("title")
        if name:
            out[code] = name
    return out


def _decorate(eng, institution, result, moved=None, failed_unit=None):
    """
    Attach unit names and move markers to a period listing from `api`.

    The listing itself, including elective slots and failed attempts, comes
    from the engine. This adds only what a reader needs and a caller checking a
    plan does not: what each unit is called, and whether the repair moved it.
    """
    rules = eng.rules[institution]
    names = _unit_names(eng, institution)
    moved = moved or {}

    periods = []
    for period in result.get("periods") or []:
        units = []
        for code in period["units"]:
            base = R.base_code(str(code))
            units.append({"code": base,
                          "name": names.get(base),
                          "credit_points": period.get("unit_cp", {}).get(base, 6),
                          "moved": moved.get(base),
                          "failed": base == failed_unit})
        periods.append({"period": period["period"],
                        "index": period.get("index"),
                        "units": units,
                        "slots": period.get("slots") or [],
                        "failed_attempts": period.get("failed_attempts") or [],
                        "credit_points": period["credit_points"]})

    out = dict(result)
    out["periods"] = periods
    # The page marks an overloaded period, and the threshold for that is the
    # institution's rule rather than a number the page should know. The two
    # happen to agree in this dataset and there is no reason they must.
    out["standard_credit_points"] = rules.min_cp_per_period
    out["maximum_credit_points"] = rules.max_cp_per_period
    return out


def plan_view(eng, institution, key):
    """The published plan, with names attached."""
    result = api.get_plan(eng, institution, key)
    if not result.get("ok"):
        return result
    return _decorate(eng, institution, result)


def replan_view(eng, institution, key, failed_unit, from_schedule=None):
    """
    A repair, with names attached and the moves marked on the plan itself.

    `from_schedule` is what makes a second failure expressible. Without it a
    repair always starts from the published plan, so a student who had already
    failed something could not ask what a further failure does to the plan they
    are actually on, and failing the same unit twice could not be asked at all.
    """
    if from_schedule is not None:
        result = api.replan_from(eng, institution, key, from_schedule, failed_unit)
    else:
        result = api.replan_after_failure(eng, institution, key, failed_unit)
    if not result.get("ok"):
        return result

    names = _unit_names(eng, institution)
    moved = {m["unit"]: {"from": m["from"], "to": m["to"]}
             for m in (result.get("moved") or [])}

    published = api.get_plan(eng, institution, key)
    out = _decorate(eng, institution, result, moved=moved, failed_unit=failed_unit)
    out["title"] = published.get("title")
    out["failed_unit_name"] = names.get(failed_unit)
    out["original_finishes_in"] = (published["periods"][-1]["period"]
                                   if published.get("periods") else None)
    return out


def arrangement_view(eng, institution, key, schedule):
    """A schedule the student arranged themselves, judged by the same validator."""
    result = api.validate_arrangement(eng, institution, key, schedule)
    if not result.get("ok"):
        return result
    published = api.get_plan(eng, institution, key)
    out = _decorate(eng, institution, result)
    out["title"] = published.get("title")
    return out



def plan_units(eng, institution, key):
    """Every unit in a plan, for the 'which unit did you fail' picker."""
    result = api.get_plan(eng, institution, key)
    if not result.get("ok"):
        return result
    names = _unit_names(eng, institution)
    units = []
    for period in result["periods"]:
        for code in period["units"]:
            base = R.base_code(str(code))
            units.append({"code": base, "name": names.get(base),
                          "period": period["period"]})
    return {"ok": True, "units": units}


def pair_list(eng):
    """The Path A pairings, labelled for a chooser."""
    out = []
    for usyd_key, uts_key in pathA.pairs(eng):
        degree = eng.plans["USYD"][usyd_key].get("degree") or usyd_key
        out.append({"degree": degree, "usyd_key": usyd_key, "uts_key": uts_key,
                    "usyd_title": eng.plans["USYD"][usyd_key].get("title") or usyd_key,
                    "uts_title": eng.plans["UTS"][uts_key].get("title") or uts_key})
    return {"ok": True, "pairs": out}


# ---------------------------------------------------------------- routes

def route(eng, path, query, body, allow_model):
    """One request. Returns (status, payload)."""
    q = lambda name, default=None: (query.get(name) or [default])[0]

    if path == "/api/health":
        return 200, {"ok": True, "plans": sum(len(v) for v in eng.plans.values()),
                     "institutions": sorted(eng.plans),
                     "model_enabled": allow_model}

    if path == "/api/plans":
        institution = q("institution")
        result = api.list_plans(eng, institution)
        # Grouped by institution then degree, because 42 Sydney keys in one list
        # is not a chooser. The degree comes from the plan record rather than
        # from parsing the key, so nothing here assumes a key format.
        grouped = {}
        for row in result["plans"]:
            plan = eng.plans[row["institution"]][row["key"]]
            degree = plan.get("degree") or plan.get("major") or row["key"]
            grouped.setdefault(row["institution"], {}).setdefault(
                degree, []).append({"key": row["key"], "title": row["title"]})
        return 200, {"ok": True, "grouped": grouped, "count": result["count"]}

    if path == "/api/plan":
        institution, key = q("institution"), q("key")
        if not institution or not key:
            return 400, {"ok": False, "error": "institution and key are required"}
        return 200, plan_view(eng, institution, key)

    if path == "/api/plan/units":
        institution, key = q("institution"), q("key")
        if not institution or not key:
            return 400, {"ok": False, "error": "institution and key are required"}
        return 200, plan_units(eng, institution, key)

    if path == "/api/validate":
        institution, key = q("institution"), q("key")
        if not institution or not key:
            return 400, {"ok": False, "error": "institution and key are required"}
        return 200, api.validate_plan(eng, institution, key)

    if path == "/api/replan":
        institution, key = q("institution"), q("key")
        failed = q("failed_unit")
        if not (institution and key and failed):
            return 400, {"ok": False,
                         "error": "institution, key and failed_unit are required"}
        return 200, replan_view(eng, institution, key, failed)

    if path == "/api/arrange":
        # A schedule the student rearranged, judged by the same validator that
        # judges every plan in Chapter 4. The page can move a unit; only this
        # can say whether the move is legal.
        body = body or {}
        institution, key = body.get("institution"), body.get("key")
        if not institution or not key:
            return 400, {"ok": False, "error": "institution and key are required"}
        if institution not in eng.plans or key not in eng.plans[institution]:
            return 404, {"ok": False, "error": f"no plan {key!r} at {institution}"}
        return 200, arrangement_view(eng, institution, key, body.get("schedule"))

    if path == "/api/replan-from":
        body = body or {}
        institution, key = body.get("institution"), body.get("key")
        failed = body.get("failed_unit")
        if not (institution and key and failed):
            return 400, {"ok": False,
                         "error": "institution, key and failed_unit are required"}
        if institution not in eng.plans or key not in eng.plans[institution]:
            return 404, {"ok": False, "error": f"no plan {key!r} at {institution}"}
        return 200, replan_view(eng, institution, key, failed,
                                from_schedule=body.get("schedule") or {})

    if path == "/api/pairs":
        return 200, pair_list(eng)

    if path == "/api/compare":
        usyd_key, uts_key = q("usyd_key"), q("uts_key")
        if not usyd_key or not uts_key:
            return 400, {"ok": False, "error": "usyd_key and uts_key are required"}
        if usyd_key not in eng.plans["USYD"]:
            return 404, {"ok": False, "error": f"no Sydney plan {usyd_key!r}"}
        if uts_key not in eng.plans["UTS"]:
            return 404, {"ok": False, "error": f"no UTS plan {uts_key!r}"}
        return 200, pathA.compare(eng, usyd_key, uts_key)

    if path == "/api/unit":
        institution, code = q("institution"), q("code")
        if not institution or not code:
            return 400, {"ok": False, "error": "institution and code are required"}
        return 200, api.lookup_unit(eng, institution, code.strip().upper())

    if path == "/api/ask":
        if not allow_model:
            return 503, {"ok": False, "model": False,
                         "error": "The conversation needs the local model and it "
                                  "is switched off for this run. The plan, "
                                  "comparison and repair views do not use it."}
        message = (body or {}).get("message", "").strip()
        if not message:
            return 400, {"ok": False, "error": "message is required"}
        import llm_layer as L
        backend = L.OllamaBackend()
        try:
            trace = L.answer(eng, backend, message,
                             context=(body or {}).get("context") or None)
        except Exception as exc:
            return 502, {"ok": False, "model": False,
                         "error": f"the local model did not answer: "
                                  f"{type(exc).__name__}",
                         "detail": str(exc)[:300]}
        # The structured result is returned alongside the prose on purpose. The
        # prose is the model's and the result is the engine's, and a reader
        # should be able to see which is which, which is the whole claim of
        # Section 3.9 made visible in the interface.
        return 200, {"ok": True, "model": True,
                     "stage": trace.get("stage"),
                     "reply": trace.get("reply"),
                     "profile": trace.get("resolved") or trace.get("profile"),
                     "tool_call": trace.get("tool_call"),
                     "result": trace.get("result"),
                     "grounding": trace.get("grounding"),
                     "intent_override": trace.get("intent_override"),
                     "carried_from_context": trace.get("carried_from_context") or []}

    return 404, {"ok": False, "error": f"no such endpoint: {path}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "curriculum-planner"
    allow_model = True
    root = None

    def log_message(self, fmt, *args):
        if os.environ.get("PLANNER_VERBOSE"):
            super().log_message(fmt, *args)

    def _send(self, status, payload):
        raw = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, path):
        # Served from one directory, with the path resolved and then checked to
        # be inside it, so a crafted path cannot read the rest of the disk.
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(STATIC, rel))
        if not target.startswith(os.path.realpath(STATIC) + os.sep) and \
                target != os.path.realpath(os.path.join(STATIC, "index.html")):
            self._send(403, {"ok": False, "error": "forbidden"})
            return
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.isfile(target):
            # A missing static directory is the one failure a person is likely
            # to hit while setting this up, and "not found" tells them nothing
            # about which of the two halves did not arrive.
            if not os.path.isdir(STATIC):
                self._send(404, {
                    "ok": False,
                    "error": "The static directory is missing, so there is no "
                             "page to serve. The engine is running: /api/health "
                             "will answer.",
                    "expected_at": STATIC,
                    "fix": "Copy the static folder itself, not its contents, "
                           "next to server.py. It should hold index.html, "
                           "app.css and app.js."})
                return
            self._send(404, {"ok": False, "error": f"not found: {rel}"})
            return
        kind = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            raw = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/api/"):
            self._handle(parsed.path, urllib.parse.parse_qs(parsed.query), None)
        else:
            self._static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "body is not JSON"})
            return
        if parsed.path == "/api/ask/stream":
            self._ask_stream(body)
            return
        self._handle(parsed.path, urllib.parse.parse_qs(parsed.query), body)

    def _ask_stream(self, body):
        """
        The same exchange as /api/ask, reported as it happens.

        One JSON object per line, which is enough structure for a reader and
        needs no framing beyond the newline. A step goes out as each stage
        finishes and the explanation goes out as the model produces it, so a
        person watching sees the work in the order it is done rather than a
        spinner followed by everything at once.

        This calls the same `llm_layer.answer` as the non-streaming endpoint
        rather than reimplementing the sequence, because two code paths through
        the same stages would eventually disagree about what those stages are.
        """
        if not self.allow_model:
            self._send(503, {"ok": False, "model": False,
                             "error": "The conversation needs the local model "
                                      "and it is switched off for this run."})
            return
        message = (body or {}).get("message", "").strip()
        if not message:
            self._send(400, {"ok": False, "error": "message is required"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            try:
                self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                raise                      # the reader left; stop the exchange

        import llm_layer as L
        backend = L.OllamaBackend()
        try:
            emit({"step": {"who": "model", "title": "Sent to the model",
                           "body": "reading the request"}})
            trace = L.answer(
                engine(self.root), backend, message,
                on_step=lambda who, title, text: emit(
                    {"step": {"who": who, "title": title, "body": text}}),
                on_text=lambda piece: emit({"text": piece}),
                context=(body or {}).get("context") or None)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            traceback.print_exc()
            try:
                emit({"done": {"ok": False, "model": False,
                               "error": f"the local model did not answer: "
                                        f"{type(exc).__name__}",
                               "detail": str(exc)[:300]}})
            except Exception:
                pass
            return

        emit({"done": {
            "ok": True, "model": True,
            "stage": trace.get("stage"),
            "reply": trace.get("reply"),
            "profile": trace.get("resolved") or trace.get("profile"),
            "tool_call": trace.get("tool_call"),
            "result": trace.get("result"),
            "grounding": trace.get("grounding"),
            "intent_override": trace.get("intent_override"),
            "carried_from_context": trace.get("carried_from_context") or [],
        }})

    def _handle(self, path, query, body):
        try:
            status, payload = route(engine(self.root), path, query, body,
                                    self.allow_model)
        except Exception as exc:
            # The trace goes to the console, not to the browser. A student
            # seeing a stack trace learns nothing; a reader of the console is
            # the person who can act on it.
            traceback.print_exc()
            status, payload = 500, {"ok": False,
                                    "error": f"{type(exc).__name__}: {exc}"}
        self._send(status, payload)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default, so the dataset is not served to "
                         "the network without being asked for")
    ap.add_argument("--no-model", action="store_true",
                    help="serve the engine views only and refuse the "
                         "conversation endpoint")
    args = ap.parse_args()

    Handler.allow_model = not args.no_model

    # Checked before anything is served, because a page that cannot be served
    # is better reported here than as a 404 in a browser.
    missing = [name for name in ("index.html", "app.css", "app.js")
               if not os.path.isfile(os.path.join(STATIC, name))]
    if missing:
        print(f"the static directory is incomplete: {STATIC}")
        print(f"  missing: {', '.join(missing)}")
        print("  copy the static folder itself, not its contents, next to "
              "server.py.")
        print("  the API will still answer; only the page is affected.\n")

    eng = engine()
    total = sum(len(v) for v in eng.plans.values())
    print(f"loaded {total} enrolment plans: " + ", ".join(
        f"{len(v)} {k}" for k, v in sorted(eng.plans.items())))
    if args.no_model:
        print("the conversation is switched off; the engine views are unaffected")
    print(f"\n  open http://{args.host}:{args.port}\n")
    print("  ctrl-c to stop")

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
