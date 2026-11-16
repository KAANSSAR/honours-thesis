"""
The language model layer: intake, tool calls, explanation. Nothing else.

Section 3.9 states the limit as "the model never decides whether a plan is
valid". This module is where that limit either holds or quietly stops holding,
so it is built so that the limit is a property of the code rather than of the
prompt. Three things follow from that, and each is a deliberate choice:

1. The model's intake output is parsed into a fixed schema and rejected if it
   does not fit. A profile that fails the schema is an intake failure that gets
   recorded, not something the engine is asked to interpret.

2. The model selects a tool and its arguments, and this module executes it. The
   model is never handed a result it did not ask for, and never executes
   anything itself. A tool name outside `api.TOOLS` is refused here.

3. The model is given the engine's structured result and asked for prose. Every
   unit code and number in that prose is then checked back against the result.
   An explanation containing a code or figure the engine did not produce is
   flagged as ungrounded, which is the extrinsic hallucination of Ji et
   al. measured mechanically rather than by reading.

The third check is what makes "grounded" testable without a human or a second
model judging it. It is deliberately strict and will sometimes flag prose that a
reader would accept, so `check_grounding` reports what it found rather than
editing the reply, and the evaluation counts flags rather than silently passing.

Backends are pluggable because Ollama runs on the student's own machine. The
deterministic half of this module is therefore testable anywhere, which is the
point: if these checks needed a running model, they could not be part of a
reproducible result.
"""

import json
import re

import api

MODEL_NAME = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/generate"

# Deliberately non-zero only where the thesis says so. Section 4.1.5 fixes
# decoding at temperature zero so that a difference between two runs is
# attributable to the prompt rather than to sampling.
TEMPERATURE = 0.0

# Qwen3 is a reasoning model and emits a <think> block before every answer. For
# all three jobs here that block is pure latency: intake is extraction, tool
# choice is a lookup against a fixed list, and explanation is rephrasing a result
# the engine already computed. None of them benefits from the model reasoning
# aloud first, and on a laptop each block costs minutes. "/no_think" is Qwen3's
# own switch for suppressing it.
NO_THINK = "/no_think"

# A thinking block can run for several minutes on an M1-class machine, so the
# default has to be generous enough that a slow reply is not mistaken for a
# hang. With NO_THINK in place most calls return in seconds.
DEFAULT_TIMEOUT = 300


# ---------------------------------------------------------------- backends

class StubBackend:
    """
    A scripted backend for testing the layer without a model.

    Takes a list of replies and returns them in order. Everything in this module
    except the model's own wording can be tested against it, which is how the
    boundary checks stay reproducible on a machine with no Ollama.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate(self, prompt, timeout=DEFAULT_TIMEOUT, on_chunk=None):
        self.calls.append(prompt)
        if not self.replies:
            raise AssertionError("StubBackend ran out of scripted replies.")
        reply = self.replies.pop(0)
        if on_chunk:
            on_chunk(reply)
        return reply


class OllamaBackend:
    """The real backend. Qwen3-8B through a local Ollama server."""

    def __init__(self, model=MODEL_NAME, url=OLLAMA_URL, temperature=TEMPERATURE,
                 timeout=DEFAULT_TIMEOUT, no_think=True, num_predict=None):
        self.model, self.url, self.temperature = model, url, temperature
        self.timeout, self.no_think = timeout, no_think
        self.last_meta = {}
        # An upper bound on the reply length, in tokens. Left unset for intake
        # and explanation, which are short by construction. The baseline of
        # Section 4.5.5 sets it, because a request for a whole eight-period plan
        # is the one job here that can run away: there is no natural stopping
        # point in a list of placements, and a model that keeps going does not
        # time out quickly, it occupies the machine for as long as it is allowed.
        # A cap turns that into a truncated reply, which the baseline counts as
        # unparseable, rather than into a hang that costs the whole run.
        self.num_predict = num_predict

    def generate(self, prompt, timeout=None, on_chunk=None):
        """
        One completion, with the model's reasoning suppressed at the API rather
        than in the prompt text.

        `/no_think` in the prompt was the original mechanism and it does not
        work on this server. Ollama parses a thinking model's reasoning out of
        the reply itself and returns it in a separate `thinking` field, so the
        text never carries a `<think>` tag for the prompt-level switch or the
        regex below to act on. Short requests appeared to work because the
        reasoning finished and the answer followed it; a request for a whole
        enrolment plan does not finish, so the entire token budget was spent in
        `thinking` and `response` came back empty. The evaluation recorded that
        as an unparseable plan, which charged the model for a planning failure
        when it had not planned at all.

        `think: false` is the server's own switch for this and it is what the
        temperature-zero, no-reasoning setting of Section 4.1.6 requires. An
        Ollama build that does not know the parameter rejects the request, so
        the call is retried without it and the prompt-level switch is left in
        place as the fallback for that case.
        """
        import requests
        if self.no_think and NO_THINK not in prompt:
            prompt = f"{prompt}\n{NO_THINK}"
        options = {"temperature": self.temperature}
        if self.num_predict:
            options["num_predict"] = self.num_predict

        body = {"model": self.model, "prompt": prompt, "stream": False,
                "options": options}
        if self.no_think:
            body["think"] = False

        # Streaming is used only when a caller asks to see the reply arrive.
        # The text produced is the same either way; what changes is whether the
        # caller waits for all of it before seeing any. Nothing downstream reads
        # the partial text: the grounding check and every other test run on the
        # complete reply, so a streamed answer and a waited-for one are judged
        # identically.
        if on_chunk is not None:
            body["stream"] = True
            return self._stream(body, timeout, on_chunk)

        response = requests.post(self.url, json=body,
                                 timeout=timeout or self.timeout)
        if response.status_code == 400 and "think" in body:
            body.pop("think")
            response = requests.post(self.url, json=body,
                                     timeout=timeout or self.timeout)
        response.raise_for_status()
        payload = response.json()

        # Kept so a caller can say why a reply was empty instead of guessing.
        self.last_meta = {"done_reason": payload.get("done_reason"),
                          "eval_count": payload.get("eval_count"),
                          "prompt_eval_count": payload.get("prompt_eval_count"),
                          "thinking_chars": len(payload.get("thinking") or ""),
                          "suppressed_reasoning": "think" in body}

        text = payload.get("response") or ""
        # Older builds inline the reasoning in the reply instead of splitting it
        # out, so the tag strip is still needed for those.
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()

    def _stream(self, body, timeout, on_chunk):
        """
        The same completion, handed over as it arrives.

        Ollama returns one JSON object per line when streaming. Each carries the
        next piece of the reply; the last carries the counters. The pieces are
        accumulated and the whole text returned, so the caller gets exactly what
        the non-streaming path would have given it and `on_chunk` is purely a
        way to watch it happen.
        """
        import requests
        pieces = []
        with requests.post(self.url, json=body, stream=True,
                           timeout=timeout or self.timeout) as response:
            if response.status_code == 400 and "think" in body:
                body = {k: v for k, v in body.items() if k != "think"}
                return self._stream(body, timeout, on_chunk)
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                piece = payload.get("response") or ""
                if piece:
                    pieces.append(piece)
                    on_chunk(piece)
                if payload.get("done"):
                    self.last_meta = {
                        "done_reason": payload.get("done_reason"),
                        "eval_count": payload.get("eval_count"),
                        "prompt_eval_count": payload.get("prompt_eval_count"),
                        "thinking_chars": len(payload.get("thinking") or ""),
                        "suppressed_reasoning": "think" in body}
        text = "".join(pieces)
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


# ---------------------------------------------------------------- intake

PROFILE_FIELDS = {
    "institution": (str, type(None)),
    "plan_key": (str, type(None)),
    "plan_mention": (str, type(None)),
    # A comparison names two degrees, and a schema with one slot for them forces
    # the model either to drop one or to stall. Both were observed: the
    # measurement in Section 4.5.3 recorded null plan keys on every comparison
    # and a timeout on five of fourteen runs, with the model producing a long
    # reply rather than a profile. The second slot is the schema admitting what
    # the request actually contains.
    "second_plan_mention": (str, type(None)),
    "failed_units": list,
    "completed_units": list,
    "intent": str,
    "unit_of_interest": (str, type(None)),
}

INTENTS = {"get_plan", "validate_plan", "replan_after_failure",
           "lookup_unit", "compare_degrees", "list_plans", "unclear"}

INTAKE_PROMPT = """You convert a student's message into JSON for a degree planning engine.

Reply with ONE JSON object and nothing else. No explanation, no markdown fences.

Fields:
  institution       "USYD", "UTS", or null if the student did not say, or null if
                    the student is asking about both. Any mention of the
                    university sets this, however brief and even with no degree
                    named: "the Sydney one", "at USyd", "the Sydney side" are
                    all "USYD", and "the UTS one", "at UTS" are all "UTS". Only
                    a message that names neither, or that asks about both at
                    once, leaves this null
  plan_key          the exact plan name if the student quoted one, else null
  plan_mention      how the student described their degree, in their own words,
                    e.g. "my software degree", else null. Keep the WHOLE
                    description, including any major, stream, specialisation or
                    elective choice they named: "Mechatronic with the Robotics
                    and Intelligent Systems specialisation", not "Mechatronic".
                    Several plans can share a degree name and only the
                    specialisation tells them apart, so dropping it makes the
                    degree unidentifiable. A reference to the university on its
                    own is NOT a degree description: "the Sydney one", "the UTS
                    one", "the Sydney side" name a university and no degree, so
                    they set institution and leave this null
  second_plan_mention  if the student named a SECOND degree, as when comparing two
                    universities, how they described that one. Else null
  failed_units      what the student says they failed, as written: codes like
                    41039 or names like "Programming 1". Else []
  completed_units   the same for units they say they have passed, else []
  intent            one of: get_plan, validate_plan, replan_after_failure,
                    lookup_unit, compare_degrees, list_plans, unclear
  unit_of_interest  the one unit the question is about, code or name as written,
                    else null

Rules:
  - Copy unit names and codes exactly as the student wrote them. Do NOT convert a
    name into a code or guess a code. Something else looks codes up.
  - Record only what the student said. Their background, entry pathway, marks and
    goals are not fields here and must not change any field that is.
  - Use "unclear" if you cannot tell what is being asked.

Examples. Note that a university on its own fills institution and nothing else,
and that the figures and names in your answer must come from the message you are
given and never from these examples:

  "now just show me the Sydney one"
  {{"institution":"USYD","plan_key":null,"plan_mention":null,
   "second_plan_mention":null,"failed_units":[],"completed_units":[],
   "intent":"unclear","unit_of_interest":null}}

  "and the UTS one?"
  {{"institution":"UTS","plan_key":null,"plan_mention":null,
   "second_plan_mention":null,"failed_units":[],"completed_units":[],
   "intent":"unclear","unit_of_interest":null}}

  "how is the unit structure different?"
  {{"institution":null,"plan_key":null,"plan_mention":null,
   "second_plan_mention":null,"failed_units":[],"completed_units":[],
   "intent":"unclear","unit_of_interest":null}}

Student message:
{message}

JSON:"""


# The two universities as a student writes them. Used only to repair the one
# field below, so there is nothing here that a description of a degree, a unit
# or a student could match.
NAMES_USYD = re.compile(
    r"\b(?:usyd|sydney uni\w*|university of sydney|sydney)\b", re.I)
NAMES_UTS = re.compile(
    r"\b(?:uts|university of technology(?: sydney)?)\b", re.I)


def repair_institution(profile, message):
    """
    Fill `institution` when the message names one university and intake did not.

    Intake is told that any mention of a university sets this field, with worked
    examples, and the 8B model still leaves it null on a short follow-up: "now
    just show me the Sydney one" came back with every field empty. The field is
    load-bearing, because it is what tells a follow-up that steps onto one side
    of a comparison from one that is still comparing both, so a model that
    forgets it turns a request for one degree into a comparison of two.

    This is a repair of intake rather than a way around it, in the same place
    and the same spirit as the JSON repair above, and it is recorded as a repair
    so that how often the model needed it stays countable.

    Note on the boundary, and on how narrow this had to become. Section 3.8
    undertakes that the engine's inputs come from the fields intake extracted
    and not from the message, so that a student's account of themselves cannot
    reach plan selection through the back door. An earlier version of this
    function ran on every request, and the probe of `biasprobe.py` showed that
    undertaking breaking at once: "I moved to Sydney last year" selected the
    University of Sydney, as did "I did my HSC in Sydney", and "my brother went
    to UTS and said it was good" selected UTS. Each of those is biography rather
    than enrolment, and a plan chosen from biography is the exact failure the
    commitment names. None of the fifteen personas happened to trip it, which is
    a warning about the persona set rather than a defence of the function.

    So it now runs only where its one failure lives: a follow-up to a comparison,
    where the previous turn already resolved both plan keys from the dataset and
    the only thing this can do is choose which of those two the student meant. It
    cannot select a plan, because both plans were selected before it ran, and on
    a first request it does not run at all.
    """
    if profile is None or profile.get("institution"):
        return profile, None

    # UTS is matched first and taken out of the text before Sydney is looked
    # for, because the university's full name ends in the other university's
    # name: "the University of Technology Sydney version" names one university
    # and would otherwise look like it names both.
    text = message or ""
    uts = bool(NAMES_UTS.search(text))
    usyd = bool(NAMES_USYD.search(NAMES_UTS.sub(" ", text)))
    if usyd == uts:            # neither named, or both: nothing to settle
        return profile, None
    profile["institution"] = "USYD" if usyd else "UTS"
    return profile, profile["institution"]


def extract_profile(backend, message, allow_repair=True):
    """
    Turn a student's message into a structured profile.

    Returns (profile, report). `report` records what happened, because intake is
    the step Logic-LM identified as the weak point of an LLM-plus-solver system
    and Section 4.1 measures it separately from the engine's own correctness. A
    failure here must therefore be visible as an intake failure rather than
    surfacing later as a bad plan.

    One repair attempt is allowed, mirroring Logic-LM's repair step, and whether
    it was needed is recorded. Logic-LM's own finding is that repair fixes syntax
    without guaranteeing that an executable formalisation is a faithful one, so a
    repaired profile is counted as repaired, never as clean.
    """
    report = {"raw": None, "repaired": False, "errors": []}
    prompt = INTAKE_PROMPT.format(message=message)

    for attempt in range(2 if allow_repair else 1):
        raw = backend.generate(prompt)
        report["raw"] = raw
        profile, errors = _parse_profile(raw)
        if not errors:
            report["errors"] = []
            return profile, report
        report["errors"] = errors
        if attempt == 0 and allow_repair:
            report["repaired"] = True
            prompt = (INTAKE_PROMPT.format(message=message)
                      + f"\n\nYour previous reply was rejected: {'; '.join(errors)}\n"
                        "Reply with corrected JSON only.")

    return None, report


def _parse_profile(raw):
    """Strict parse. Returns (profile, errors); a non-empty errors list means no profile."""
    errors = []
    text = raw.strip()

    # Models wrap JSON in fences even when told not to. Stripping a fence is a
    # formatting allowance, not an interpretation of content.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None, ["no JSON object in the reply"]
    text = text[start:end + 1]

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return None, [f"invalid JSON: {e.msg}"]
    if not isinstance(obj, dict):
        return None, ["top-level JSON value is not an object"]

    profile = {}
    for field, expected in PROFILE_FIELDS.items():
        value = obj.get(field, [] if expected is list else None)
        if expected is list:
            value = value or []
            if not isinstance(value, list):
                errors.append(f"{field} is not a list")
                value = []
            value = [str(v).strip() for v in value if str(v).strip()]
        elif not isinstance(value, expected):
            errors.append(f"{field} should be {expected}, got {type(value).__name__}")
            value = None
        profile[field] = value

    intent = (profile.get("intent") or "unclear")
    if intent not in INTENTS:
        errors.append(f"intent {intent!r} is not one of {sorted(INTENTS)}")
    profile["intent"] = intent

    inst = profile.get("institution")
    if inst is not None:
        inst = str(inst).upper()
        if inst not in {"USYD", "UTS"}:
            errors.append(f"institution {inst!r} is neither USYD nor UTS")
            inst = None
        profile["institution"] = inst

    return (None, errors) if errors else (profile, [])


def settle_intent(profile):
    """
    Decide the intent by rule wherever the fields already decide it.

    Section 4.5.3 measured the same request asked by seven students who differed
    only in background, and found the intent moving between `validate_plan` and
    `replan_after_failure` for two of the seven in two separate scenarios. The
    engine cannot cause that: it is never shown the student's background. It
    entered through the model's reading of the request, which is the risk
    Section 3.8 names and the reason this function exists.

    The distinction it got wrong is not a judgement. A student who states a
    failed unit and asks what happens to their plan is asking for a repair, and
    that is readable from the fields themselves without weighing anything. So it
    is decided here, in code, where it cannot vary by who is asking. The model
    keeps the part that genuinely needs reading: which degree, which unit, which
    of those fields the message filled at all.

    Returns (profile, override) where `override` records any change, so that the
    evaluation can report how often the rule had to correct the model rather
    than hiding the correction behind a better score.
    """
    before = profile.get("intent")
    intent = before

    # A stated failure plus a plan question is a repair request.
    if profile.get("failed_units") and intent in {"get_plan", "validate_plan",
                                                  "unclear"}:
        intent = "replan_after_failure"

    # Two degrees named is a comparison, whichever of the two slots they landed in.
    if profile.get("second_plan_mention") and intent in {"get_plan",
                                                         "validate_plan",
                                                         "list_plans", "unclear"}:
        intent = "compare_degrees"

    # A unit named with no degree and no failure attached is a lookup.
    if (intent == "unclear" and profile.get("unit_of_interest")
            and not profile.get("failed_units")
            and not (profile.get("plan_key") or profile.get("plan_mention"))):
        intent = "lookup_unit"

    profile["intent"] = intent
    return profile, (None if intent == before else {"from": before, "to": intent})


# ---------------------------------------------------------------- resolution

def resolve_profile(engine, profile):
    """
    Turn the student's own words into codes and plan keys, using the dataset.

    Intake deliberately captures what the student wrote rather than converting it,
    because a model asked to produce a unit code it has not been shown will invent
    one. Converting "Programming 1" into 41039 is a lookup over data the engine
    already holds, so it happens here, deterministically, and anything that cannot
    be resolved is reported rather than guessed at.

    Returns (resolved, report). `resolved` is a copy of the profile with codes and
    a plan key filled in where they could be determined; `report` lists what could
    not be resolved, with candidates, so the reply can ask a useful question
    instead of failing silently.
    """
    resolved = dict(profile)
    report = {"unresolved": [], "ambiguous": []}
    institution = profile.get("institution")

    def note(field, text, outcome):
        entry = {"field": field, "text": text,
                 "candidates": outcome.get("candidates") or [],
                 "error": outcome.get("error")}
        (report["ambiguous"] if outcome.get("ambiguous")
         else report["unresolved"]).append(entry)

    # A unit's prerequisites are a property of the unit, not of the degree the
    # student happens to be in, so a lookup is answered without a plan and
    # without asking for one. The code also identifies its own institution, so
    # it is resolved against whichever dataset holds it rather than against the
    # one carried over from the previous question. Before this, a question about
    # AMME3500 asked during a conversation about a UTS degree was answered with
    # "which plan are you on", which is a question the answer does not depend on.
    if profile.get("intent") == "lookup_unit" and profile.get("unit_of_interest"):
        outcome, where = api.resolve_unit_anywhere(
            engine, institution or "USYD", profile["unit_of_interest"])
        if outcome["ok"]:
            resolved["unit_of_interest"] = outcome["code"]
            resolved["institution"] = where
        else:
            resolved["unit_of_interest"] = None
            note("unit_of_interest", profile["unit_of_interest"], outcome)
        return resolved, report

    if institution:
        for field in ("failed_units", "completed_units"):
            codes = []
            for text in profile.get(field) or []:
                outcome = api.resolve_unit(engine, institution, text)
                if outcome["ok"]:
                    codes.append(outcome["code"])
                else:
                    note(field, text, outcome)
            resolved[field] = codes

        if profile.get("unit_of_interest"):
            outcome = api.resolve_unit(engine, institution,
                                       profile["unit_of_interest"])
            resolved["unit_of_interest"] = (outcome["code"] if outcome["ok"]
                                            else None)
            if not outcome["ok"]:
                note("unit_of_interest", profile["unit_of_interest"], outcome)

        if not profile.get("plan_key"):
            # A failed unit pins the plan down better than a vague description
            # does, so try the description first and fall back to asking.
            mention = profile.get("plan_mention") or profile.get("plan_key")
            outcome = api.resolve_plan(engine, institution, mention)
            if outcome["ok"]:
                resolved["plan_key"] = outcome["key"]
            else:
                note("plan_key", mention, outcome)
        else:
            outcome = api.resolve_plan(engine, institution, profile["plan_key"])
            if outcome["ok"]:
                resolved["plan_key"] = outcome["key"]
            else:
                note("plan_key", profile["plan_key"], outcome)
    else:
        report["unresolved"].append(
            {"field": "institution", "text": None,
             "candidates": ["USYD", "UTS"]})

    return resolved, report


def resolve_comparison(engine, profile):
    """
    Both plan keys for a comparison, taken from the dataset's own pairing.

    A comparison is the one request where the institution is legitimately null,
    because there are two of them, and `resolve_profile` therefore resolves
    nothing for it. Section 4.5.3 measured the consequence: the model was asked
    to supply two plan keys it had never been shown, and on every run of both
    comparison scenarios it either passed nulls or invented a key, so the engine
    refused all fourteen. That is not an inconsistency in the model's reading, it
    is a gap in this layer, and it is closed here rather than in the prompt.

    The pairing is already in the dataset: `major_map_to_usyd` names which UTS
    major corresponds to which Sydney degree, and Path A reads it. So the two
    keys are a lookup, like a unit code, and belong on this side of the boundary.

    Matching is done against the fields intake extracted, never against the raw
    message. The message carries the student's own account of themselves, and
    reaching back into it would put that account into the engine's input through
    the back door, which is the thing Section 3.8 undertakes not to do.
    """
    import pathA
    available = pathA.pairs(engine)
    text = " ".join(str(part).lower() for part in
                    (profile.get("plan_mention"),
                     profile.get("second_plan_mention"),
                     profile.get("plan_key")) if part)

    hits, labels = [], []
    for usyd_key, uts_major in available:
        degree = (engine.plans["USYD"][usyd_key].get("degree") or "").lower()
        if degree and re.search(rf"\b{re.escape(degree)}\b", text):
            hits.append((usyd_key, uts_major))
        labels.append(engine.plans["USYD"][usyd_key].get("degree") or usyd_key)

    if len(hits) == 1:
        usyd_key, uts_major = hits[0]
        return {"ok": True, "usyd_key": usyd_key, "uts_key": uts_major}
    if len(hits) > 1:
        return {"ok": False, "ambiguous": True,
                "error": "that names more than one degree",
                "candidates": [engine.plans["USYD"][k].get("degree") or k
                               for k, _ in hits]}
    return {"ok": False, "error": "no degree named that I can compare",
            "candidates": sorted(set(labels))}


def describe_gaps(report):
    """A question a student can answer, built from what could not be resolved."""
    parts = []
    for entry in report["ambiguous"]:
        options = ", ".join(entry["candidates"][:4])
        parts.append(f"{entry['text']!r} could be any of: {options}")
    for entry in report["unresolved"]:
        if entry["field"] == "institution":
            parts.append("which university you are at, UTS or the University of Sydney")
        elif entry["field"] == "unit_of_interest" and entry.get("error"):
            # The resolver's own message says what went wrong with more precision
            # than this function can reconstruct from the text alone: that a code
            # is not that university's, as against a name it could not place.
            parts.append(entry["error"].rstrip("."))
        elif entry["text"]:
            parts.append(f"I could not find {entry['text']!r}")
        else:
            options = ", ".join(entry["candidates"][:4])
            parts.append(f"which plan you are on ({options})" if options
                         else "which plan you are on")
    return "; ".join(parts)


# ---------------------------------------------------------------- tool calls

TOOL_PROMPT = """You choose which engine function to call. You do not answer the question yourself.

Available functions:
  list_plans()
  get_plan(institution, key)
  validate_plan(institution, key)
  replan_after_failure(institution, key, failed_unit)
  lookup_unit(institution, code)
  compare_degrees(usyd_key, uts_key)

Reply with ONE JSON object and nothing else:
  {{"tool": "<name>", "args": {{...}}}}

Structured profile of the student's request:
{profile}

JSON:"""


def choose_tool(backend, profile):
    """
    Ask the model which engine function to call, and validate the choice here.

    The model picks; this function decides whether the pick is executable. A tool
    outside `api.TOOLS` or an argument set that does not match the function's
    signature is refused before anything runs, so a malformed tool call cannot
    reach the engine and cannot be mistaken for an engine failure later.
    """
    raw = backend.generate(TOOL_PROMPT.format(profile=json.dumps(profile, indent=1)))
    report = {"raw": raw, "errors": []}

    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        report["errors"].append("no JSON object in the reply")
        return None, report
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        report["errors"].append(f"invalid JSON: {e.msg}")
        return None, report

    name = obj.get("tool")
    args = obj.get("args") or {}
    if name not in api.TOOLS:
        report["errors"].append(f"{name!r} is not an available function")
        return None, report
    if not isinstance(args, dict):
        report["errors"].append("args is not an object")
        return None, report

    import inspect
    accepted = set(inspect.signature(api.TOOLS[name]).parameters) - {"engine"}
    unknown = set(args) - accepted
    if unknown:
        report["errors"].append(f"{name} does not accept {sorted(unknown)}")
        return None, report

    # The model picks the function. It does not get to pick the arguments.
    #
    # Until here the check was on the argument names only, so a call naming
    # `key` was accepted whatever string sat in it, and `run_tool` then executed
    # that string against the engine. Every one of these values has already been
    # resolved from the dataset in `resolve_profile`, which is the step that
    # exists precisely so the model never has to produce a plan key or a unit
    # code it was not shown. Leaving the model's own version in place made that
    # resolution advisory: a request whose plan could not be resolved, and which
    # should therefore have been answered with a question, could still reach the
    # engine with a key the model supplied, and come back as a confident answer
    # about a degree the student never named.
    #
    # So each argument the resolver has a value for is replaced by that value.
    # What the model wrote is kept in the report rather than discarded, because
    # the number of times it differs is a measurement of how far the model's
    # choice can drift, and Section 4.1 counts it.
    resolved_args = {
        "institution": profile.get("institution"),
        "key": profile.get("plan_key"),
        "code": profile.get("unit_of_interest"),
        "failed_unit": (profile.get("failed_units") or [None])[0],
        # `answer` builds the comparison call itself, so these only matter when
        # the model reaches for compare_degrees on a follow-up the intent rules
        # left unclear. The pair the dataset chose last time is still the pair.
        "usyd_key": profile.get("compared_usyd_key"),
        "uts_key": profile.get("compared_uts_key"),
    }
    overridden = {}
    for field, value in resolved_args.items():
        if field in accepted and value is not None and args.get(field) != value:
            overridden[field] = {"model": args.get(field), "resolved": value}
            args[field] = value
    report["overridden_args"] = overridden

    # The complement of the override. The gate in `answer` asks for a missing
    # plan only when the intent is one that obviously needs one, so a message
    # read as "unclear" skips it, and the model was then free to pick `get_plan`
    # and fill the key in itself. A key the resolver does not have is a key the
    # dataset did not supply, whoever wrote it, so the call is refused and the
    # student is asked instead.
    for field in ("key", "code", "failed_unit", "usyd_key", "uts_key"):
        if field in accepted and resolved_args[field] is None:
            report["errors"].append(
                f"{name} needs {field}, and it was not resolved from the dataset")
            return None, report

    return {"tool": name, "args": args}, report


def run_tool(engine, call):
    """Execute a validated tool call. The only place an engine function is invoked."""
    func = api.TOOLS[call["tool"]]
    try:
        return func(engine, **call["args"])
    except TypeError as e:
        return {"ok": False, "error": f"{call['tool']} rejected those arguments: {e}"}


# ---------------------------------------------------------------- explanation

EXPLAIN_PROMPT = """Answer the student's question using the engine's result.

Use ONLY the facts in the result below. Do not add unit codes, numbers, dates or
requirements that are not there. If the result says no plan was found, say so
plainly and do not suggest one.

How to write it:
  - First sentence answers the question that was asked. Do not open by restating
    the question or by saying what the result is.
  - Quote the actual unit codes and numbers from the result, as digits. Set one
    side's figure against the other's where the result has both. A sentence that
    would be true of any two degrees, such as saying that their requirements
    differ, is not an answer and tells the student nothing.
  - Every figure you write must come from the result below. Do not carry any
    number out of these instructions into your reply.
  - If the question asks about one thing, answer about that thing. Leave out the
    parts of the result it did not ask about.
  - A field ending in "_in_words" already states a rule correctly. Say what it
    says. "AND" between two units means both are required, not either of them,
    and "none recorded" means the dataset holds no such rule, which is not the
    same as the unit having none.
  - Two to five sentences, prose, no bullet points, no headings.

The student asked: {question}

Engine result:
{result}

Reply:"""

# A number that is part of a unit code must not be read as a standalone figure,
# so codes are matched first and removed before numbers are considered.
USYD_CODE = re.compile(r"\b[A-Z]{2,5}\d{3,4}\b")
UTS_CODE = re.compile(r"\b\d{5}\b")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

# A figure written as a word is still a figure, and the digit pattern above does
# not see it. Asked how two degrees differed in structure, the model answered
# "thirteen of its units have requisites against Sydney's seven" when the result
# said twenty-two on both sides, and the reply passed the grounding check because
# neither number appeared as a digit anywhere in it. Only the words for ten and
# above are listed, because anything below ten is ordinary prose and is not
# checked either when written as a digit.
UNITS_WORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
              "seven": 7, "eight": 8, "nine": 9}
TEENS_WORD = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
              "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
              "eighteen": 18, "nineteen": 19}
TENS_WORD = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
             "seventy": 70, "eighty": 80, "ninety": 90}
NUMBER_WORD = re.compile(
    r"\b(" + "|".join(list(TEENS_WORD) + list(TENS_WORD) + list(UNITS_WORD)
                      + ["hundred", "and"]) + r")\b", re.I)


def spelled_numbers(text):
    """
    The values of any numbers ten or above that `text` writes as words.

    Runs of number words are read as one figure, so "one hundred and ninety
    eight" is 198 and not 1, 90 and 8. Getting that wrong matters in the
    direction of a false alarm: the engine really does return 198 credit points,
    and reading the phrase as ninety-eight would put a warning on a correct
    answer, which is worse than the gap this function was written to close.

    "and" is consumed inside a run but cannot start or extend one on its own, so
    an ordinary "and" between two sentences does not join their numbers.
    """
    found, run = set(), []

    def flush():
        if not run:
            return
        total = chunk = 0
        for word in run:
            if word in UNITS_WORD:
                chunk += UNITS_WORD[word]
            elif word in TEENS_WORD:
                chunk += TEENS_WORD[word]
            elif word in TENS_WORD:
                chunk += TENS_WORD[word]
            elif word == "hundred":
                chunk = (chunk or 1) * 100
                total += chunk
                chunk = 0
        total += chunk
        if total >= 10:
            found.add(float(total))
        run.clear()

    position = 0
    for match in NUMBER_WORD.finditer(text or ""):
        word = match.group(1).lower()
        # A gap holding anything but spaces, hyphens or a comma ends the run,
        # which keeps "six units, twelve credit points" as two figures.
        between = (text or "")[position:match.start()]
        if run and between.strip(" \t\n-,"):
            flush()
        if word == "and" and not run:
            position = match.end()
            continue
        run.append(word)
        position = match.end()
    flush()
    return found

# The early prototype's failure was an invented support email address, not an
# invented number (Section 3.9). A contact detail is the most harmful thing this
# layer can fabricate, because a student may act on it directly, so it is checked
# by its own pattern rather than being left to the code and number checks.
CONTACT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"                 # email
    r"|https?://\S+|www\.\S+"                  # url
    r"|\b(?:\+?61|0)[\s-]?\d(?:[\s-]?\d){7,9}\b"  # phone
)


def for_explanation(result):
    """
    A compact view of the result for the explanation prompt.

    A repaired plan carries both `moved` and the full `periods` listing, which is
    several hundred tokens of input the model has to read before writing two
    sentences. `moved` already says everything an explanation needs about a
    repair, so the period listing is dropped and a long move list is capped.

    Grounding is still checked against the full result, never this view. Trimming
    what the model sees can only narrow what it has to draw on, so it cannot
    smuggle an unsupported claim past the check.
    """
    if not isinstance(result, dict):
        return result
    brief = dict(result)

    # A unit's requisite is in the result twice: as the structured groups the
    # validator reads, and as the sentence `describe_prereq` renders from them.
    # The groups are an AND of ORs, and asked for AMME3500's prerequisites from
    # [["AMME2000"], ["AMME2500"]] the model answered "either AMME2000 or
    # AMME2500", which is the opposite of the rule. The sentence says it once and
    # unambiguously, so that is what the model is shown. Grounding is still
    # checked against the full result, and the sentence names the same codes, so
    # nothing the model may quote has been hidden from the check.
    if brief.get("prerequisite_in_words"):
        for field in ("prerequisite_groups", "prerequisite_alternatives",
                      "corequisites", "credit_point_requirements"):
            brief.pop(field, None)

    if "moved" in brief:
        brief.pop("periods", None)
        if len(brief["moved"]) > 10:
            brief["moved"] = brief["moved"][:10]
            brief["moved_truncated"] = True
    return brief


def explain(backend, question, result, on_text=None):
    """Turn a structured engine result into prose, then check the prose against it."""
    reply = backend.generate(
        EXPLAIN_PROMPT.format(question=question,
                              result=json.dumps(for_explanation(result), indent=1)),
        on_chunk=on_text)
    # Checked on the whole reply, never on a partial one, so a streamed answer
    # is judged exactly as a waited-for answer is.
    return reply, check_grounding(reply, result)


def check_grounding(reply, result):
    """
    Every unit code and number in `reply` must appear in `result`.

    This is a necessary condition for a grounded explanation, not a sufficient
    one: it catches invented codes and invented figures, which is the failure the
    early prototype showed and the one that matters for a student acting on the
    advice. It cannot catch a reply that misdescribes a correctly quoted number,
    which is why Section 4.1 reports it alongside a hand-scored subset rather
    than on its own.

    Returns a report rather than a verdict, so the caller decides what to do and
    the evaluation can count flags.
    """
    blob = json.dumps(result)
    source_codes = set(USYD_CODE.findall(blob)) | set(UTS_CODE.findall(blob))
    source_numbers = set(NUMBER.findall(blob))

    reply_codes = set(USYD_CODE.findall(reply)) | set(UTS_CODE.findall(reply))
    stripped = UTS_CODE.sub(" ", USYD_CODE.sub(" ", reply))
    reply_numbers = set(NUMBER.findall(stripped))

    unsupported_codes = sorted(reply_codes - source_codes)
    # Small integers are ordinary prose ("two of your units", "a year or so"),
    # so flagging them would make the check useless. Only figures large enough to
    # be a claim about the plan are treated as needing support.
    unsupported_numbers = sorted(n for n in reply_numbers - source_numbers
                                 if float(n) >= 10)

    # The same test for a figure the reply spelled out. It is compared by value
    # against the digits in the result, so "twenty-two" is supported by a 22 in
    # the result and "thirteen" is not supported by anything.
    supported_values = {float(n) for n in source_numbers}
    unsupported_numbers += [
        f"{value:g}" for value in sorted(spelled_numbers(reply) - supported_values)]
    # A URL inside the JSON blob is followed by a closing quote, which the url
    # pattern's \S+ swallows, so both sides are trimmed of trailing punctuation
    # before being compared. Without this a correctly quoted source URL is
    # flagged as invented.
    def contacts(text):
        return {m.strip().rstrip('".,;:)]}\'') for m in CONTACT.findall(text)}

    unsupported_contacts = sorted(contacts(reply) - contacts(blob))

    return {"grounded": not (unsupported_codes or unsupported_numbers
                             or unsupported_contacts),
            "unsupported_codes": unsupported_codes,
            "unsupported_numbers": unsupported_numbers,
            "unsupported_contacts": unsupported_contacts,
            "codes_in_result": len(source_codes)}


# ---------------------------------------------------------------- the exchange

CARRIED = ("institution", "plan_key", "plan_mention", "second_plan_mention",
           "intent")


def _describe_profile(profile):
    """A one-line account of what intake understood, for the step list."""
    if not profile:
        return "could not read it"
    bits = []
    if profile.get("institution"):
        bits.append(profile["institution"])
    if profile.get("plan_key") or profile.get("plan_mention"):
        bits.append(str(profile.get("plan_key") or profile.get("plan_mention")))
    if profile.get("failed_units"):
        bits.append("failed " + ", ".join(profile["failed_units"]))
    if profile.get("unit_of_interest"):
        bits.append("about " + str(profile["unit_of_interest"]))
    return " \u00b7 ".join(bits) if bits else "nothing specific"


def _describe_result(result):
    """What the engine returned, in one line, for the step list."""
    if not isinstance(result, dict):
        return "returned a result"
    if result.get("ok") is False:
        return result.get("error") or "refused the request"
    if result.get("units_moved") is not None:
        return (f"{result['units_moved']} units moved, finishes "
                f"{result.get('finishes_in')}")
    if result.get("valid") is not None:
        return ("the plan satisfies every recorded rule" if result["valid"]
                else f"{result.get('violation_count')} rule(s) not satisfied")
    if result.get("usyd") and result.get("uts"):
        return "compared the two degrees"
    if result.get("unit"):
        return f"the record for {result['unit']}"
    return "returned a result"


def _grounding_step(grounding):
    """The grounding verdict as a step."""
    if grounding.get("grounded") is False:
        quoted = (grounding.get("unsupported_codes") or []) + \
                 (grounding.get("unsupported_numbers") or [])
        return ("flag", "The wording was flagged",
                f"quoted {', '.join(quoted)}, which the engine did not produce")
    return ("rule", "Checked the wording",
            "every code and figure in the reply appears in the engine's answer")


def carry_context(profile, context):
    """
    Fill the degree the student did not repeat, from the one they named before.

    A follow-up like "what if I fail it twice" names no degree, and without this
    it can only be answered with a question the student has already answered.

    The rule is narrower than it first looks, because a loose version of it is
    worse than none. "Show me the plan for Mechatronic at Sydney" leaves
    `plan_key` empty and puts the degree in `plan_mention`, so filling any empty
    field independently pasted the previous UTS software plan over a question
    about a Sydney mechatronic one. The student had named a degree; it simply
    had not been resolved to a key yet.

    So the plan is carried only when the message mentions no degree at all, and
    the institution only when the message names neither a university nor a
    degree, since a degree named without its university should be resolved from
    the degree rather than from wherever the student was last. A unit lookup
    carries nothing but the institution: which plan they are on does not change
    what a unit's prerequisites are.
    """
    if not profile or not context:
        return profile, []

    names_a_degree = bool(profile.get("plan_key") or profile.get("plan_mention")
                          or profile.get("second_plan_mention"))
    taken = []

    if not profile.get("institution") and not names_a_degree and context.get("institution"):
        profile["institution"] = context["institution"]
        taken.append("institution")

    # The pair a comparison settled on stays available for as long as the
    # conversation does not start a different one. These two came from the
    # dataset's own pairing rather than from anything the student said, so
    # carrying them adds no reading of the student to the engine's input, and
    # without it a second follow-up loses the pair that the first one had.
    if not profile.get("second_plan_mention"):
        for field in ("compared_usyd_key", "compared_uts_key"):
            if context.get(field) and not profile.get(field):
                profile[field] = context[field]

    # Stepping out of a comparison onto one side of it. The previous question
    # resolved both keys from the dataset's own pairing, so the side this
    # message names is already known exactly and does not have to be resolved
    # again from a description that may match fifteen plans.
    side = {"USYD": "compared_usyd_key", "UTS": "compared_uts_key"}.get(
        profile.get("institution") or "")
    if (side and not names_a_degree and not profile.get("plan_key")
            and context.get(side)):
        profile["plan_key"] = context[side]
        taken.append("plan_key")
        # With the key known there is nothing left for the model to choose, and
        # leaving the intent unclear sent the request to the tool step, where
        # the model could reach for a comparison again. A failure named in the
        # same breath is still a repair, so only a reading of "unclear" is
        # settled here.
        if profile.get("intent") == "unclear" and not profile.get("failed_units"):
            profile["intent"] = "get_plan"
            taken.append("intent")
        return profile, taken

    if profile.get("intent") == "lookup_unit":
        return profile, taken

    if not names_a_degree:
        for field in ("plan_key", "plan_mention", "second_plan_mention"):
            if context.get(field):
                profile[field] = context[field]
                taken.append(field)

        # "How is the unit structure different?" asked straight after a
        # comparison is a question about that comparison, but it names no degree
        # and no second degree, so intake reads it as unclear and the rule in
        # `settle_intent` has nothing to fire on. Both degrees have just been
        # carried above, so the intent that goes with them is carried too.
        #
        # Only an unclear intent is overridden, and only when this message named
        # no university either. "Now just show me the Sydney one" is read as
        # unclear and would otherwise have been carried back into a comparison,
        # which is the opposite of what it asks: naming one university is how a
        # student steps out of a comparison and onto one side of it. A real
        # comparison follow-up, "how is the unit structure different", names
        # neither university, which is why it still carries.
        if (profile.get("intent") == "unclear"
                and context.get("intent") == "compare_degrees"
                and profile.get("second_plan_mention")
                and not profile.get("institution")
                and not profile.get("failed_units")):
            profile["intent"] = "compare_degrees"
            taken.append("intent")

    return profile, taken


# For each intent, the one function it can mean and the resolved fields that
# function needs. When the intent is settled and every field here has a value,
# there is nothing left to choose: the call is determined, and asking the model
# to produce it adds ways to get it wrong and none to get it more right.
#
# This is what the earlier fixes to the comparison and the lookup were, written
# once instead of three times. It also closes the hole those two did not: an
# intent of get_plan still went through the model, which was free to answer it
# by calling compare_degrees instead, and on a follow-up to a comparison it did
# exactly that. "Now just show me the Sydney one" came back as a comparison of
# both degrees with the Sydney half described, which is a different question
# from the one that was asked even when the figures in it are right.
DETERMINED = {
    "get_plan": ("get_plan", {"institution": "institution", "key": "plan_key"}),
    "validate_plan": ("validate_plan",
                      {"institution": "institution", "key": "plan_key"}),
    "replan_after_failure": ("replan_after_failure",
                             {"institution": "institution", "key": "plan_key",
                              "failed_unit": "failed_units"}),
    "lookup_unit": ("lookup_unit", {"institution": "institution",
                                    "code": "unit_of_interest"}),
}


def determined_call(profile):
    """The engine call a settled intent already fixes, or None if it does not."""
    entry = DETERMINED.get(profile.get("intent"))
    if not entry:
        return None
    tool, wiring = entry
    args = {}
    for argument, field in wiring.items():
        value = profile.get(field)
        if field == "failed_units":
            value = (value or [None])[0]
        if value is None:
            return None
        args[argument] = value
    return {"tool": tool, "args": args}


def answer(engine, backend, message, on_step=None, on_text=None, context=None):
    """
    One full exchange: intake, tool call, execution, explanation.

    Returns everything each stage produced, because Section 4.1 measures the
    stages separately and a single pass/fail would hide which one failed. The
    engine result is carried through untouched, so a caller that does not trust
    the prose can read the structured answer instead, which is also what the
    frontend does.

    `on_step` is called as each stage finishes, and `on_text` with the
    explanation as it arrives. Both are for watching; neither changes what is
    computed, and the returned trace is the same whether they are given or not.

    `context` is the resolved profile from the caller's previous exchange, and
    it is how a follow-up question works at all. It is deliberately the
    structured profile rather than a transcript: carrying the conversation would
    make the answer depend on everything said before it, and the property this
    system is built to have is that the same request gets the same answer.
    Carrying the resolved fields keeps that, because the pair of (context,
    message) determines the result exactly. Only fields the new message left
    empty are filled, nothing is overwritten, and every field taken this way is
    reported so a reader can see what was assumed rather than asked.
    """
    trace = {"message": message, "stage": None}
    step = on_step or (lambda *a, **k: None)

    profile, intake = extract_profile(backend, message)
    trace["intake"] = intake
    trace["profile"] = profile
    step("intake", "Read the request",
         _describe_profile(profile) if profile else "could not read it")
    if profile is None:
        trace["stage"] = "intake_failed"
        trace["reply"] = ("I could not read that as a planning request. Could you say "
                          "which degree and which unit you mean?")
        return trace

    # What kind of request this is, wherever the extracted fields decide it. The
    # model's reading of the intent was the one stage that moved with the
    # student's background, so the part of it that is decidable by rule is
    # decided here and the override is recorded.
    # Settled before anything is carried, because the rules that settle the
    # intent read what this message said and nothing else. Carrying first would
    # let a degree inherited from the previous question change what this one is
    # taken to be asking.
    # Only a follow-up to a comparison may have its institution read out of the
    # message, and only because the previous turn already chose both plans. See
    # `repair_institution` for what went wrong when this ran on every request.
    if context and (context.get("compared_usyd_key")
                    or context.get("compared_uts_key")):
        profile, filled = repair_institution(profile, message)
        trace["institution_repaired"] = filled
        if filled:
            step("rule", "Read the university from your question",
                 f"{filled}, because the last answer compared two degrees and "
                 f"this one names a side")

    profile, override = settle_intent(profile)
    trace["intent_override"] = override
    if override:
        step("rule", "Corrected the intent",
             f"{override['from']} to {override['to']}, decided by rule because "
             f"the extracted fields already settle it")

    profile, carried = carry_context(profile, context)
    trace["carried_from_context"] = carried
    if carried:
        step("context", "Carried over from your last question",
             ", ".join(f"{f.replace('_', ' ')} {profile[f]}" for f in carried))

    # Names the student wrote become codes and plan keys here, from the dataset,
    # before the model is asked to choose a function. Without this the model is
    # handed a request it can only answer by inventing a code.
    profile, resolution = resolve_profile(engine, profile)
    trace["resolved"] = profile
    trace["resolution"] = resolution
    step("rule", "Resolved against the dataset",
         (f"intent {profile['intent']}"
          + (f", matched to {profile['plan_key']}" if profile.get("plan_key") else "")))

    # A comparison names two degrees at two universities, so it is resolved
    # against the dataset's own pairing rather than left to the model, which has
    # not been shown either key. The engine is then called directly: there is
    # exactly one function this intent can mean, so asking the model to pick it
    # would add a way to get it wrong and no way to get it more right.
    if profile["intent"] == "compare_degrees":
        pair = resolve_comparison(engine, profile)
        trace["comparison"] = pair
        if not pair["ok"]:
            options = ", ".join(pair.get("candidates", [])[:6])
            trace["stage"] = "needs_clarification"
            # Deliberately does not fall back to the pair the conversation was
            # last comparing. A follow-up that failed to name a degree is more
            # likely to have named one this function cannot pair than to have
            # meant the previous one, and quietly answering about the previous
            # one is the kind of confident wrong answer the whole design is
            # arranged to avoid. Asking costs the student one turn.
            trace["reply"] = (
                f"I compare one degree at Sydney against its UTS counterpart, "
                f"and {pair['error']}. Which degree did you mean? "
                f"I can do {options}."
                if options else
                f"I compare one degree at Sydney against its UTS counterpart, "
                f"and {pair['error']}.")
            return trace
        # Kept on the resolved profile so that a follow-up naming one of the two
        # universities can be answered about that side's plan exactly, rather
        # than by resolving the student's description of it a second time. The
        # description was good enough to pick a pair, not always good enough to
        # pick a plan on its own: "Software Engineering at Sydney" matches
        # fifteen Sydney plans, and only the pairing says which one was compared.
        profile["compared_usyd_key"] = pair["usyd_key"]
        profile["compared_uts_key"] = pair["uts_key"]
        trace["resolved"] = profile

        call = {"tool": "compare_degrees",
                "args": {"usyd_key": pair["usyd_key"], "uts_key": pair["uts_key"]}}
        trace["tool_call"] = call
        step("engine", "Called compare_degrees",
             f"{pair['usyd_key']} against {pair['uts_key']}")
        trace["tool_report"] = {"raw": None, "errors": [],
                                "resolved_without_model": True}
        result = run_tool(engine, call)
        trace["result"] = result
        step("engine", "The engine answered", _describe_result(result))
        reply, grounding = explain(backend, message, result, on_text=on_text)
        trace["reply"] = reply
        trace["grounding"] = grounding
        step(*_grounding_step(grounding))
        trace["stage"] = "answered" if result.get("ok") else "engine_refused"
        return trace

    # A request missing the things the engine needs is answered with a question,
    # not with a lookup for a plan named "None". Asking is a real answer here.
    needs_plan = profile["intent"] in {"get_plan", "validate_plan",
                                       "replan_after_failure"}
    if (needs_plan and not profile.get("plan_key")) or resolution["ambiguous"]:
        gaps = describe_gaps(resolution)
        trace["stage"] = "needs_clarification"
        trace["reply"] = (f"I need one more detail before I can answer: {gaps}."
                          if gaps else
                          "I need to know which degree and plan you are on.")
        return trace
    if profile["intent"] == "replan_after_failure" and not profile["failed_units"]:
        trace["stage"] = "needs_clarification"
        trace["reply"] = ("Which unit did you fail? A code like 41039 or its name "
                          "both work.")
        return trace
    if profile["intent"] == "lookup_unit" and not profile.get("unit_of_interest"):
        gaps = describe_gaps(resolution)
        trace["stage"] = "needs_clarification"
        trace["reply"] = (f"{gaps}." if gaps else
                          "Which unit did you mean? A code like 41039 or AMME3500, "
                          "or the unit's name, both work.")
        return trace

    # An intent whose arguments are all resolved determines its call, so it is
    # built here rather than asked for. Everything the model could still add at
    # this point is a way to diverge from what the dataset already settled.
    call = determined_call(profile)
    if call:
        trace["tool_call"] = call
        trace["tool_report"] = {"raw": None, "errors": [],
                                "resolved_without_model": True}
        step("engine", f"Called {call['tool']}",
             ", ".join(f"{k}={v}" for k, v in call["args"].items()))
        result = run_tool(engine, call)
        trace["result"] = result
        step("engine", "The engine answered", _describe_result(result))
        reply, grounding = explain(backend, message, result, on_text=on_text)
        trace["reply"] = reply
        trace["grounding"] = grounding
        step(*_grounding_step(grounding))
        trace["stage"] = "answered" if result.get("ok") else "engine_refused"
        return trace

    call, tool_report = choose_tool(backend, profile)
    trace["tool_call"] = call
    trace["tool_report"] = tool_report
    if call:
        step("engine", f"Called {call['tool']}",
             ", ".join(f"{k}={v}" for k, v in (call.get("args") or {}).items())
             or "no arguments")
    if call is None:
        # A call refused for a missing argument is a different thing from a call
        # the model could not form at all, and the student can do something
        # about the first one. Saying "rephrase it" to someone who only has to
        # name their degree wastes their turn.
        missing = {"key": "which degree and plan you are on",
                   "code": "which unit you mean",
                   "failed_unit": "which unit you failed"}
        wanted = [phrase for field, phrase in missing.items()
                  if any(f"needs {field}" in e for e in tool_report["errors"])]
        trace["stage"] = "needs_clarification" if wanted else "tool_call_failed"
        trace["reply"] = (
            f"I need one more detail before I can answer: {', and '.join(wanted)}."
            if wanted else
            "I could not work out which part of the planner to use for that. "
            "Could you rephrase it?")
        return trace

    result = run_tool(engine, call)
    trace["result"] = result
    step("engine", "The engine answered", _describe_result(result))

    reply, grounding = explain(backend, message, result, on_text=on_text)
    trace["reply"] = reply
    trace["grounding"] = grounding
    step(*_grounding_step(grounding))
    trace["stage"] = "answered" if result.get("ok") else "engine_refused"
    return trace
