"""
The engine's operations, exposed as plain functions.

This is the layer Section 3.10 describes: the language model and the web frontend
call the same four groups of operations, so neither can reach a capability the
other does not have, and neither can reach the solver directly.

Every function here is deterministic. Nothing in this module calls a language
model, and nothing in it accepts free text. A caller that wants to act on a
student's words has to turn them into the arguments below first, which is the
intake step of `llm_layer.py` and the one place the model is permitted to decide
anything. The boundary matters for the thesis claim rather than only for tidiness:
if the model could reach these functions with free text, "the model never decides
validity" would stop being true the moment a prompt changed.

Results are JSON-serialisable dicts. Each carries an `ok` flag, so a caller never
has to distinguish an exception from a refusal, and a refusal always names the
reason in `error` rather than returning something that looks like a plan.
"""

import json
import os
import re

import collections

import core
import rules as R
import rules_usyd
import rules_uts
import datasets
from paths import P

USYD_PLANS = P("usyd_default_plans.json")
UTS_PLANS = P("uts_default_plans.json")
UTS_DATA = P("uts_c09066_data.json")

# USYD plan files name the degree in short form; datasets.py wants the full name.
USYD_DEGREE_ALIAS = {"Software": "Software Engineering"}


# ---------------------------------------------------------------- loading

class Engine:
    """
    Holds the plan stores and the per-institution datasets.

    Loaded once and reused, because building the requisite graph for every call
    would dominate the cost of a conversational exchange. The stores stay
    separate by institution throughout, exactly as the pipeline keeps them:
    a requisite that cannot be resolved inside its own institution's store is a
    violation, never a lookup in the other one.
    """

    def __init__(self, root=None):
        # `root` is accepted for older callers and ignored: every file is found
        # through paths.P, so the engine runs from any working directory.
        self.root = root
        self.plans = {}
        self.data = {}
        self.rules = {}
        self._graphs = {}

        usyd_path = USYD_PLANS
        if os.path.exists(usyd_path):
            plans = json.load(open(usyd_path))["plans"]
            self.plans["USYD"] = plans
            self.rules["USYD"] = rules_usyd.USYDRules()
            for degree in {p["degree"] for p in plans.values()}:
                name = USYD_DEGREE_ALIAS.get(degree, degree)
                ds = datasets.load("USYD", name)
                self.data.setdefault("USYD", {})[degree] = {
                    "units": ds.units, "blocks": ds.blocks}

        uts_path = UTS_PLANS
        if os.path.exists(uts_path):
            self.plans["UTS"] = json.load(open(uts_path))["plans"]
            self.rules["UTS"] = rules_uts.UTSRules()
            self.data["UTS"] = json.load(open(UTS_DATA))

    # -- internals ------------------------------------------------------

    def _dataset_for(self, institution, plan):
        if institution == "USYD":
            return self.data["USYD"][plan["degree"]]
        return self.data["UTS"]

    def _graph(self, institution, plan):
        """Requisite graph for a plan's dataset, built once per dataset."""
        key = (institution, plan["degree"] if institution == "USYD" else "UTS")
        if key not in self._graphs:
            self._graphs[key] = core.build_requisite_graph(
                self._dataset_for(institution, plan))
        return self._graphs[key]

    def _schedule_for(self, institution, plan):
        import run_all_plans as H
        return (H.usyd_schedule(plan) if institution == "USYD"
                else H.uts_schedule(plan))


# ---------------------------------------------------------------- group 1

def list_plans(engine, institution=None):
    """Every enrolment plan the system holds, as selectable keys."""
    out = []
    for inst, plans in engine.plans.items():
        if institution and inst != institution:
            continue
        for key, plan in plans.items():
            out.append({"institution": inst, "key": key,
                        "title": plan.get("title") or plan.get("major") or key})
    return {"ok": True, "plans": sorted(out, key=lambda p: (p["institution"], p["key"])),
            "count": len(out)}


def get_plan(engine, institution, key):
    """
    The enrolment plan for one degree and specialisation combination.

    Returns the plan as periods in teaching order, labelled in the institution's
    own terms, because a caller that has to know the engine's position arithmetic
    to read a plan is a caller that can get it wrong.
    """
    plans = engine.plans.get(institution)
    if not plans:
        return {"ok": False, "error": f"No plans loaded for {institution!r}."}
    if key not in plans:
        return {"ok": False, "error": f"No enrolment plan called {key!r} at {institution}.",
                "did_you_mean": _closest(key, plans)}

    plan = plans[key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    schedule = engine._schedule_for(institution, plan)

    # Same listing the repair returns, so the two views of a plan cannot
    # describe it differently. They did: this one reported open slots and the
    # repair silently dropped them.
    periods = _periods_view(rules, schedule, data)

    return {"ok": True, "institution": institution, "key": key,
            "title": plan.get("title") or plan.get("major") or key,
            "schedule": {str(i): [str(c) for c in schedule[i]]
                         for i in sorted(schedule) if schedule[i]},
            "periods": periods,
            "total_credit_points": sum(p["credit_points"] for p in periods)}


# ---------------------------------------------------------------- group 2

def validate_plan(engine, institution, key, completed=None):
    """
    Check an enrolment plan against its own institution's rules.

    `completed` lets a caller state units already passed, which only ever removes
    violations. It cannot add any, so it is safe to accept from a profile the
    model produced.
    """
    plans = engine.plans.get(institution) or {}
    if key not in plans:
        return {"ok": False, "error": f"No enrolment plan called {key!r} at {institution}."}

    plan = plans[key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    prereqs, coreqs, _ = engine._graph(institution, plan)
    schedule = engine._schedule_for(institution, plan)

    violations = core.validate_schedule(rules, schedule, prereqs, coreqs, data)
    return {"ok": True, "institution": institution, "key": key,
            "valid": not violations,
            "violation_count": len(violations),
            "violations": [_violation(v) for v in violations]}


# ---------------------------------------------------------------- group 3

def replan_after_failure(engine, institution, key, failed_unit):
    """
    Repair a plan after a failed unit, and report what moved.

    The repaired plan is re-validated here rather than being trusted, so a caller
    cannot be handed a plan that the engine itself would reject. `moved` is what
    an explanation should actually talk about: a student wants to know which of
    their units changed, not that a search succeeded.
    """
    plans = engine.plans.get(institution) or {}
    if key not in plans:
        return {"ok": False, "error": f"No enrolment plan called {key!r} at {institution}."}

    plan = plans[key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    prereqs, coreqs, dependents = engine._graph(institution, plan)
    before = engine._schedule_for(institution, plan)

    code = failed_unit.strip().upper() if institution == "USYD" else failed_unit.strip()
    if code not in {c for v in before.values() for c in v}:
        return {"ok": False,
                "error": f"{code} is not in the {key!r} plan, so there is nothing to replan.",
                "valid": None}

    try:
        after = core.replan_after_failure(
            rules, before, code, data, prereqs, dependents, coreqs)
    except RuntimeError as e:
        # The engine could not place a unit inside its search horizon. Reported
        # as a refusal rather than an exception, because "no plan fits" is a real
        # answer a student is entitled to (Section 3.6.6) and not a crash.
        return {"ok": False, "error": str(e), "valid": False, "no_plan_found": True}

    violations = core.validate_schedule(rules, after, prereqs, coreqs, data)
    pos_before = {c: i for i, v in before.items() for c in v if not R.is_placeholder(c)}
    pos_after = {c: i for i, v in after.items() for c in v if not R.is_placeholder(c)}

    moved = []
    for unit in sorted(set(pos_before) | set(pos_after)):
        was, now = pos_before.get(unit), pos_after.get(unit)
        if was != now:
            moved.append({
                "unit": unit,
                "from": rules.position_label(was) if was is not None else None,
                "to": rules.position_label(now) if now is not None else None})

    last_before = max(i for i in before if R.is_real(i) and before[i])
    last_after = max(i for i in after if R.is_real(i) and after[i])

    return {"ok": True, "institution": institution, "key": key,
            "failed_unit": code,
            "valid": not violations,
            "violations": [_violation(v) for v in violations],
            "units_moved": len(moved),
            "moved": moved,
            "extends_plan": last_after > last_before,
            "finishes_in": rules.position_label(last_after),
            "schedule": {str(i): [str(c) for c in after[i]]
                         for i in sorted(after) if after[i]},
            "periods": _periods_view(rules, after, data)}


def _periods_view(rules, schedule, data):
    """
    A period listing that accounts for everything occupying the period.

    Written because the repair's own listing did not. It filtered placeholders
    and the failed attempt out of `units` while `credit_points` went on counting
    them, so a repaired Biomedical plan showed a Spring session of 24 credit
    points holding two subjects worth twelve, and the elective slots a student
    still has to fill simply disappeared the moment they reported a failure. The
    numbers were right and the list was not, which is the worse way round.
    """
    out = []
    for index in sorted(schedule):
        if not schedule[index]:
            continue
        units, slots, failed = [], [], []
        for code in schedule[index]:
            text = str(code)
            # Order matters: `is_placeholder` is true of a failed attempt as
            # well as an elective slot, so the failure is tested for first.
            if text.startswith(R.FAILED_PREFIX):
                failed.append({"unit": R.base_code(text),
                               "credit_points": rules.unit_cp(code, data)})
            elif R.is_placeholder(text):
                pool = text[len(R.ELECTIVE_PREFIX):].strip() or "elective"
                slots.append({"pool": pool,
                              "credit_points": rules.unit_cp(code, data)})
            else:
                units.append(text)
        out.append({
            "period": rules.position_label(index),
            "index": index,
            "units": sorted(units),
            "slots": slots,
            "open_slots": len(slots),
            "failed_attempts": failed,
            "credit_points": core.period_credit_points(rules, schedule, index, data)})
    return out


def _schedule_from(engine, institution, key, raw):
    """
    Turn a schedule sent by a caller back into one the engine can read.

    Only codes the plan already contains are accepted, and only positions that
    exist, so a caller can rearrange a plan but cannot invent units, duplicate
    them or smuggle in a code from another degree. What comes back is checked
    against the dataset rather than trusted, because this is the one entry point
    where a schedule arrives from outside the engine.
    """
    plan = engine.plans[institution][key]
    published = engine._schedule_for(institution, plan)
    allowed = collections.Counter(str(c) for v in published.values() for c in v)

    schedule, seen = {}, collections.Counter()
    for index, codes in (raw or {}).items():
        try:
            i = int(index)
        except (TypeError, ValueError):
            return None, f"{index!r} is not a position"
        if i < 0 or i > R.MAX_SEARCH_HORIZON:
            return None, f"position {i} is outside the plan"
        for code in codes or []:
            text = str(code)
            # A failed attempt is not part of the published plan and there can
            # be several of the same unit, one per attempt. What it may not be
            # is a unit the degree does not contain.
            if text.startswith(R.FAILED_PREFIX):
                if R.base_code(text) not in allowed:
                    return None, f"{R.base_code(text)} is not a unit of this plan"
                schedule.setdefault(i, []).append(text)
                continue
            seen[text] += 1
            if seen[text] > allowed.get(text, 0):
                return None, (f"{text} is not in this plan, or appears more "
                              f"often than the plan allows")
            schedule.setdefault(i, []).append(text)

    missing = [c for c, n in allowed.items() if seen.get(c, 0) < n]
    if missing:
        return None, (f"the schedule is missing {len(missing)} item(s) the plan "
                      f"requires, including {sorted(missing)[0]}")
    return schedule, None


def validate_arrangement(engine, institution, key, raw_schedule):
    """
    Check a schedule the caller arranged, rather than the published one.

    This is what lets a student move a unit and be told whether the move is
    legal. The same validator judges it as judges every plan in Chapter 4; this
    function only gets the schedule into the right shape first.
    """
    plans = engine.plans.get(institution) or {}
    if key not in plans:
        return {"ok": False, "error": f"No enrolment plan called {key!r} at {institution}."}

    plan = plans[key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    prereqs, coreqs, _ = engine._graph(institution, plan)

    schedule, problem = _schedule_from(engine, institution, key, raw_schedule)
    if problem:
        return {"ok": False, "error": problem}

    violations = core.validate_schedule(rules, schedule, prereqs, coreqs, data)
    last = max((i for i in schedule if R.is_real(i) and schedule[i]), default=0)
    return {"ok": True, "institution": institution, "key": key,
            "valid": not violations,
            "violation_count": len(violations),
            "violations": [_violation(v) for v in violations],
            "finishes_in": rules.position_label(last),
            "periods": _periods_view(rules, schedule, data)}


def replan_from(engine, institution, key, raw_schedule, failed_unit):
    """
    Repair from the schedule the caller is looking at, not the published plan.

    `replan_after_failure` always starts from the published sequence, so a
    second failure could not be expressed at all: a student who fails a unit,
    takes the repaired plan and then fails something else, or the same unit
    again, had no way to ask about it. The engine itself was never the
    limitation, since `core.replan_after_failure` takes whatever schedule it is
    given and counts the overload budget from the failure markers already in it.
    Only this layer was pinned to the published plan.
    """
    plans = engine.plans.get(institution) or {}
    if key not in plans:
        return {"ok": False, "error": f"No enrolment plan called {key!r} at {institution}."}

    plan = plans[key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    prereqs, coreqs, dependents = engine._graph(institution, plan)

    before, problem = _schedule_from(engine, institution, key, raw_schedule)
    if problem:
        return {"ok": False, "error": problem}

    code = failed_unit.strip().upper() if institution == "USYD" else failed_unit.strip()
    if code not in {str(c) for v in before.values() for c in v}:
        return {"ok": False,
                "error": f"{code} is not sitting in this plan, so there is "
                         f"nothing to replan.", "valid": None}

    try:
        after = core.replan_after_failure(
            rules, before, code, data, prereqs, dependents, coreqs)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "valid": False, "no_plan_found": True}

    violations = core.validate_schedule(rules, after, prereqs, coreqs, data)
    pos_before = {str(c): i for i, v in before.items() for c in v
                  if not R.is_placeholder(str(c))}
    pos_after = {str(c): i for i, v in after.items() for c in v
                 if not R.is_placeholder(str(c))}
    moved = []
    for unit in sorted(set(pos_before) | set(pos_after)):
        was, now = pos_before.get(unit), pos_after.get(unit)
        if was != now:
            moved.append({"unit": R.base_code(unit),
                          "from": rules.position_label(was) if was is not None else None,
                          "to": rules.position_label(now) if now is not None else None})

    last_before = max((i for i in before if R.is_real(i) and before[i]), default=0)
    last_after = max((i for i in after if R.is_real(i) and after[i]), default=0)
    attempts = sum(1 for v in after.values() for c in v
                   if str(c) == f"{R.FAILED_PREFIX}{code}")

    return {"ok": True, "institution": institution, "key": key,
            "failed_unit": code,
            "attempts_failed": attempts,
            "valid": not violations,
            "violations": [_violation(v) for v in violations],
            "units_moved": len(moved),
            "moved": moved,
            "extends_plan": last_after > last_before,
            "finishes_in": rules.position_label(last_after),
            "schedule": {str(i): [str(c) for c in after[i]]
                         for i in sorted(after) if after[i]},
            "periods": _periods_view(rules, after, data)}


def lookup_unit(engine, institution, code):
    """One unit's rules, for a question about a single unit."""
    if institution == "UTS":
        units = engine.data["UTS"]["units"]
    else:
        units = {}
        for per_degree in engine.data.get("USYD", {}).values():
            units.update(per_degree["units"])

    code = code.strip().upper() if institution == "USYD" else code.strip()
    unit = units.get(code)
    if not unit:
        return {"ok": False, "error": f"{code} is not in the {institution} dataset.",
                "did_you_mean": _closest(code, units)}

    # The rule in words, rendered by the same rules object that enforces it.
    #
    # `prerequisite_groups` is an AND of ORs, so AMME3500's [["AMME2000"],
    # ["AMME2500"]] means both units and not either of them. A reader given only
    # the field has to know that convention to state the rule correctly, and the
    # 8B model in the chat layer does not reliably know it: asked what AMME3500
    # needs, it offered the two as alternatives. The same applies to an empty
    # list, which means "none recorded" rather than "none". So the sentence is
    # produced here, by `describe_prereq`, which reads the fields the validator
    # reads. The structured fields stay, because a caller checking the answer
    # needs them and the grounding check is made against them.
    rules = (engine.rules or {}).get(institution)
    words = {}
    if rules is not None:
        shape = {"units": units}
        prereq = rules.describe_prereq(code, shape)
        coreq = rules.describe_coreq(code, shape)
        words = {
            "prerequisite_in_words": prereq or "none recorded",
            "corequisite_in_words": coreq or "none recorded",
        }

    return {"ok": True, "institution": institution, "unit": code,
            "name": unit.get("name"),
            "credit_points": unit.get("credit_points"),
            "sessions_offered": unit.get("sessions_offered") or [],
            **words,
            "prerequisite_groups": unit.get("prerequisite_groups") or [],
            "prerequisite_alternatives": unit.get("prerequisite_codes") or [],
            "corequisites": unit.get("corequisite_codes") or [],
            "prohibitions": (unit.get("prohibition_codes")
                             or unit.get("antirequisite_codes") or []),
            "credit_point_requirements": unit.get("credit_point_requirements") or [],
            "requisites_recorded": bool(
                unit.get("prerequisite_codes") or unit.get("prerequisite_groups")
                or unit.get("credit_point_requirements"))}


# ---------------------------------------------------------------- group 4

def compare_degrees(engine, usyd_key, uts_key):
    """
    Path A. Structural comparison of two plans, computed from the dataset.

    Deliberately returns numbers and no prose. Section 3.8 puts the bias claim on
    this function returning the same figures whatever the student's background,
    so it takes no profile argument at all: there is nothing here for a
    demographic attribute to influence, by construction rather than by discipline.
    """
    left = get_plan(engine, "USYD", usyd_key)
    right = get_plan(engine, "UTS", uts_key)
    for side in (left, right):
        if not side["ok"]:
            return side

    def shape(side, institution, key):
        plan = engine.plans[institution][key]
        rules = engine.rules[institution]
        data = engine._dataset_for(institution, plan)
        schedule = engine._schedule_for(institution, plan)
        prereqs, _, dependents = engine._graph(institution, plan)
        placed = [c for v in schedule.values() for c in v if not R.is_placeholder(c)]

        blocking = {}
        for unit in placed:
            seen, stack = set(), [unit]
            while stack:
                cur = stack.pop()
                for dep in dependents.get(cur, ()):  # transitive closure
                    if dep not in seen:
                        seen.add(dep)
                        stack.append(dep)
            blocking[unit] = len(seen)

        return {
            "key": key,
            "title": side["title"],
            "teaching_periods": sum(1 for p in side["periods"]
                                    if p["units"] or p["open_slots"]),
            "units_placed": len(placed),
            "open_slots": sum(p["open_slots"] for p in side["periods"]),
            "total_credit_points": sum(p["credit_points"] for p in side["periods"]),
            "units_with_requisites": sum(
                1 for c in placed
                if (data["units"].get(R.base_code(c), {}).get("prerequisite_codes")
                    or data["units"].get(R.base_code(c), {}).get("prerequisite_groups"))),
            "max_blocking_factor": max(blocking.values()) if blocking else 0,
            "most_blocking_unit": (max(blocking, key=blocking.get)
                                   if blocking else None)}

    return {"ok": True,
            "usyd": shape(left, "USYD", usyd_key),
            "uts": shape(right, "UTS", uts_key),
            "note": ("Structural comparison only. Says nothing about teaching "
                     "quality, workload or career outcomes.")}


# ---------------------------------------------------------------- resolution

def _units_for(engine, institution):
    if institution == "UTS":
        return engine.data["UTS"]["units"]
    units = {}
    for per_degree in engine.data.get("USYD", {}).values():
        units.update(per_degree["units"])
    return units


def resolve_unit(engine, institution, text):
    """
    Turn what a student wrote into a unit code.

    Students say "Programming 1", not "41039". Mapping one to the other is a
    lookup against the dataset, not a judgement, so it belongs here rather than in
    the model: asking the model to produce a code it has not been shown is asking
    it to invent one, which is the failure the whole design exists to avoid.

    Exact matches are tried before any fuzzy matching, because ten UTS subjects
    have "Programming" in their name and only one of them is "Programming 1". A
    near-match that is not clearly better than its rivals is reported as ambiguous
    with its candidates, rather than resolved to a guess.
    """
    if not text:
        return {"ok": False, "error": "nothing to resolve"}

    units = _units_for(engine, institution)
    probe = str(text).strip()

    # Already a code.
    for candidate in (probe, probe.upper()):
        if candidate in units:
            return {"ok": True, "code": candidate, "matched_on": "code"}

    # Exact name, case-insensitive.
    by_name = {}
    for code, unit in units.items():
        name = (unit.get("name") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(code)
    exact = by_name.get(probe.lower())
    if exact and len(exact) == 1:
        return {"ok": True, "code": exact[0], "matched_on": "name"}
    if exact:
        return {"ok": False, "error": f"{probe!r} matches several units",
                "candidates": sorted(exact), "ambiguous": True}

    # Fuzzy, and only when one candidate is clearly ahead of the next.
    import difflib
    scored = sorted(
        ((difflib.SequenceMatcher(None, probe.lower(), n).ratio(), n) for n in by_name),
        reverse=True)
    if scored and scored[0][0] >= 0.85 and (
            len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.05):
        codes = by_name[scored[0][1]]
        if len(codes) == 1:
            return {"ok": True, "code": codes[0], "matched_on": "approximate name",
                    "matched_name": scored[0][1]}

    # Something written as a unit code is not a misspelt unit name, so it gets
    # no name candidates. Asked for AMME3500 while the institution was set to
    # UTS, this used to answer that it could be any of 48024, 41039, 16263 or
    # 48023, which are neither close to what was asked nor the same shape as it.
    # A code that does not exist here should say so and let the caller look
    # elsewhere, which `resolve_unit_anywhere` then does.
    if LOOKS_LIKE_CODE.match(probe):
        return {"ok": False,
                "error": f"{probe.upper()} is not a {institution} unit code.",
                "candidates": [], "ambiguous": False, "looks_like_code": True}

    close = [by_name[n][0] for _, n in scored[:4] if by_name[n]]
    return {"ok": False, "error": f"No {institution} unit matches {probe!r}.",
            "candidates": close, "ambiguous": bool(close)}


# A unit code as either institution writes one: Sydney's letters and digits, or
# a UTS five-digit number. Used to tell "they typed a code" from "they typed a
# name", which decides whether a near-miss on a name is worth offering.
LOOKS_LIKE_CODE = re.compile(r"^\s*(?:[A-Za-z]{2,5}\s?\d{3,4}|\d{5})\s*$")


def resolve_unit_anywhere(engine, institution, text):
    """
    Resolve a unit, trying the other institution if it is not found here.

    A unit code identifies its institution by itself, and a student asking what
    AMME3500 requires is asking about that unit wherever the conversation
    happened to be. Without this, carrying the institution from an earlier
    question about a UTS degree made a question about a Sydney unit
    unanswerable, which is a worse failure than not carrying anything.

    The institution is only changed when the code is found in exactly one of
    them, so nothing is guessed: the dataset decides.
    """
    first = resolve_unit(engine, institution, text)
    if first.get("ok"):
        return first, institution

    other = "UTS" if institution == "USYD" else "USYD"
    if other in (engine.plans or {}):
        second = resolve_unit(engine, other, text)
        if second.get("ok"):
            return second, other
    return first, institution


def resolve_plan(engine, institution, text):
    """
    Turn what a student wrote into an enrolment plan key.

    Same reasoning as `resolve_unit`: "my software degree" is a description, and
    picking the plan it means is a lookup over keys the engine already holds. When
    the institution is known and exactly one of its plans matches, that is an
    answer; when several do, the caller is told which rather than being handed one
    of them.
    """
    plans = engine.plans.get(institution) or {}
    if not plans:
        return {"ok": False, "error": f"No plans loaded for {institution!r}."}
    if not text:
        return {"ok": False, "error": "nothing to resolve",
                "candidates": sorted(plans)[:6]}

    probe = str(text).strip()

    # "Civil Engineering major at UTS" names the plan "Civil Engineering major".
    # The trailing institution phrase is how a student says it, not part of the
    # name, and trimming it is a lookup over the text rather than a reading of
    # it. Without this the exact match misses and the word heuristic below falls
    # back to "civil", which matches four UTS plans and reports an ambiguity that
    # is not really there. Section 4.5.3 recorded exactly that: one student in
    # seven phrased the degree with the university attached and was asked a
    # clarifying question the other six were not.
    probes = [probe]
    trimmed = re.sub(
        r"\s+(?:at|from|with|in)\s+(?:uts|usyd|sydney|"
        r"the\s+university\s+of\s+sydney|university\s+of\s+sydney|"
        r"the\s+university\s+of\s+technology(?:\s+sydney)?|"
        r"university\s+of\s+technology(?:\s+sydney)?)\s*$",
        "", probe, flags=re.I).strip()
    if trimmed and trimmed.lower() != probe.lower():
        probes.append(trimmed)

    lowered = {k.lower(): k for k in plans}
    for candidate in probes:
        if candidate in plans:
            return {"ok": True, "key": candidate, "matched_on": "key"}
        if candidate.lower() in lowered:
            return {"ok": True, "key": lowered[candidate.lower()],
                    "matched_on": "key"}

    # Every word the student used has to appear in the key, which keeps "civil"
    # from quietly selecting one of the three Civil plans.
    # The words dropped here are the ones a student uses to join a degree to its
    # specialisation rather than to name either: "Mechatronic with the Robotics
    # and Intelligent Systems specialisation" names a plan whose key contains
    # neither "with" nor "specialisation", so keeping them means every word can
    # never all appear and the match falls through to approximate matching on the
    # whole phrase. Specialisation words themselves are kept, because they are
    # the only thing separating the three Mechatronic plans from each other.
    words = [w for w in re.findall(r"[a-z]+", probe.lower())
             if w not in {"my", "the", "a", "an", "degree", "course", "major", "in",
                          "at", "with", "and", "or", "of", "for", "on", "am", "im",
                          "doing", "studying", "enrolled", "bachelor", "honours",
                          "honors", "specialisation", "specialization", "stream",
                          "specialising", "specializing", "program", "programme",
                          "uts", "usyd", "sydney", "university", "engineering"}]
    if words:
        hits = [k for k in plans if all(w in k.lower() for w in words)]
        if len(hits) == 1:
            return {"ok": True, "key": hits[0], "matched_on": "description"}
        if hits:
            return {"ok": False, "error": f"{probe!r} matches several plans",
                    "candidates": sorted(hits), "ambiguous": True}

    import difflib
    close = difflib.get_close_matches(probe, list(plans), n=4, cutoff=0.5)
    return {"ok": False, "error": f"No {institution} plan matches {probe!r}.",
            "candidates": close or sorted(plans)[:6], "ambiguous": bool(close)}


# ---------------------------------------------------------------- helpers

def _violation(v):
    """Violations are tuples inside the engine; name the fields for a caller."""
    if isinstance(v, (list, tuple)):
        parts = list(v) + [None] * (3 - len(v))
        return {"kind": parts[0], "unit": parts[1], "detail": str(parts[2])}
    return {"kind": "violation", "unit": None, "detail": str(v)}


def _closest(needle, haystack, limit=3):
    """Cheap suggestion list, so a near-miss key does not read as 'does not exist'."""
    import difflib
    return difflib.get_close_matches(str(needle), [str(k) for k in haystack],
                                     n=limit, cutoff=0.6)


# The tool surface the language model is allowed to reach. Anything not named
# here is unreachable from a conversation, which is what makes the boundary
# testable rather than merely stated.
TOOLS = {
    "list_plans": list_plans,
    "get_plan": get_plan,
    "validate_plan": validate_plan,
    "replan_after_failure": replan_after_failure,
    "lookup_unit": lookup_unit,
    "compare_degrees": compare_degrees,
}
