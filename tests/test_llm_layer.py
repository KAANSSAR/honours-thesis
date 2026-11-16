"""
Tests for the language model layer's boundary.

These run without a language model, by design. The claim in Section 3.9 is that
the model cannot decide validity and cannot reach the engine with anything the
engine has not agreed to accept. That is a property of this code, so it should be
checkable without the model being present, and a result that needed a running
Ollama could not be reproduced by anyone marking the thesis.

Run: python3 test_llm_layer.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import json

import api
import llm_layer as L

PLAN = "Software Engineering major"
PROFILE = ('{"institution":"UTS","plan_key":"%s","failed_units":["41039"],'
           '"completed_units":[],"intent":"replan_after_failure",'
           '"unit_of_interest":"41039"}' % PLAN)
CALL = ('{"tool":"replan_after_failure","args":{"institution":"UTS",'
        '"key":"%s","failed_unit":"41039"}}' % PLAN)

passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ok    {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {detail}")


# ---------------------------------------------------------------- intake

def test_intake():
    print("\nintake")

    profile, report = L.extract_profile(L.StubBackend([PROFILE]), "I failed 41039")
    check("valid JSON is accepted", profile is not None)
    check("institution is normalised", profile and profile["institution"] == "UTS")
    check("failed units are carried through", profile and profile["failed_units"] == ["41039"])
    check("clean intake is not marked repaired", not report["repaired"])

    # A model that answers in prose must not silently produce an empty profile.
    profile, report = L.extract_profile(
        L.StubBackend(["Sure, here is what I think!", PROFILE]), "I failed 41039")
    check("prose then JSON is repaired, not dropped", profile is not None)
    check("the repair is recorded", report["repaired"])

    profile, report = L.extract_profile(L.StubBackend(["no", "still no"]), "???")
    check("unparseable intake yields no profile", profile is None)
    check("intake failure names a reason", bool(report["errors"]))

    # An intent the engine has no function for must not reach the engine.
    bad = '{"institution":"UTS","plan_key":null,"failed_units":[],"completed_units":[],' \
          '"intent":"enrol_me_please","unit_of_interest":null}'
    profile, report = L.extract_profile(L.StubBackend([bad, bad]), "enrol me")
    check("an invented intent is rejected", profile is None)

    # A fenced reply is a formatting quirk, not a content problem.
    fenced = "```json\n" + PROFILE + "\n```"
    profile, _ = L.extract_profile(L.StubBackend([fenced]), "I failed 41039")
    check("a fenced JSON reply is accepted", profile is not None)


# ---------------------------------------------------------------- tool calls

def test_tool_calls():
    print("\ntool calls")
    profile = json.loads(PROFILE)

    call, report = L.choose_tool(L.StubBackend([CALL]), profile)
    check("a valid call is accepted", call is not None)
    check("the tool name is carried through",
          call and call["tool"] == "replan_after_failure")

    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"drop_the_unit","args":{}}']), profile)
    check("a function that does not exist is refused", call is None)
    check("the refusal names the function", any("drop_the_unit" in e for e in report["errors"]))

    call, report = L.choose_tool(L.StubBackend([
        '{"tool":"get_plan","args":{"institution":"UTS","key":"x","student_gpa":6.5}}']), profile)
    check("an argument the function does not accept is refused", call is None)

    call, _ = L.choose_tool(L.StubBackend(["I would call get_plan"]), profile)
    check("prose instead of a call is refused", call is None)

    # The model must not be able to reach a private helper by naming it.
    call, _ = L.choose_tool(
        L.StubBackend(['{"tool":"_violation","args":{}}']), profile)
    check("a private helper is not reachable", call is None)


# ---------------------------------------------------------------- grounding

def test_grounding():
    print("\ngrounding")
    result = {"moved": [{"unit": "41039"}], "total_credit_points": 198,
              "units_moved": 2, "source_url": "https://uts.edu.au/real"}

    cases = [
        ("a clean reply passes", "Retake 41039 next Autumn.", True),
        ("an invented unit code is caught", "Also enrol in 99999.", False),
        ("an invented figure is caught", "That comes to 240 credit points.", False),
        ("an invented email is caught", "Email help@uts.edu.au about it.", False),
        ("an invented url is caught", "See https://uts.edu.au/fake.", False),
        ("an invented phone number is caught", "Call 02 9514 2000.", False),
        ("a quoted source url is not flagged",
         "The page is https://uts.edu.au/real.", True),
        ("small integers in prose are not flagged",
         "Two subjects move by one session.", True),
        ("a quoted figure is not flagged", "The total stays at 198.", True),

        # A figure written as a word used to pass whatever it said, because the
        # digit pattern cannot see it. Asked how two degrees differed, the model
        # answered "thirteen of its units have requisites against Sydney's
        # seven" where the engine had said twenty-two on both sides, and the
        # check called that grounded.
        ("an invented figure spelled out is caught",
         "Thirteen of its units have requisites.", False),
        # A run of number words is one figure. Read word by word this would be
        # 1, 90 and 8, none of which the engine produced, and a correct answer
        # would be warned about.
        ("a spelled-out figure the engine did produce is not flagged",
         "The total stays at one hundred and ninety eight.", True),
        ("a spelled-out figure the engine did not produce still is",
         "The total stays at two hundred and forty.", False),
        ("spelled-out small numbers stay unflagged",
         "Two subjects move by one session, which is three in total.", True),
    ]
    for name, reply, expected in cases:
        check(name, L.check_grounding(reply, result)["grounded"] == expected,
              str(L.check_grounding(reply, result)))

    check("twenty-two reads as 22", L.spelled_numbers("twenty-two units") == {22.0})
    check("a hyphen is not required", L.spelled_numbers("forty five") == {45.0})
    check("a teen is read as itself", L.spelled_numbers("nineteen") == {19.0})
    check("nothing below ten is reported", L.spelled_numbers("two or three") == set())
    check("a hundreds run is one figure",
          L.spelled_numbers("one hundred and ninety eight") == {198.0})
    check("and reads the same without the and",
          L.spelled_numbers("one hundred ninety eight") == {198.0})
    check("words in separate phrases stay separate figures",
          L.spelled_numbers("six units, twelve credit points") == {12.0})
    check("an ordinary and does not join two figures",
          L.spelled_numbers("one session and one unit") == set())

    # The exact reply from the transcript, against the exact result it was given.
    comparison = {"ok": True,
                  "usyd": {"units_with_requisites": 22, "units_placed": 32},
                  "uts": {"units_with_requisites": 22, "units_placed": 31}}
    verdict = L.check_grounding(
        "The unit structure differs in their requirements, with thirteen of its "
        "units having requisites against Sydney's seven.", comparison)
    check("the transcript's fabricated comparison is now flagged",
          verdict["grounded"] is False and "13" in verdict["unsupported_numbers"])
    check("and the figure the engine did give passes",
          L.check_grounding(
              "Both sides have twenty-two units with requisites.",
              comparison)["grounded"])


# ---------------------------------------------------------------- end to end

def test_exchange():
    print("\nfull exchange")
    engine = api.Engine()

    trace = L.answer(engine, L.StubBackend(
        [PROFILE, CALL, "You failed 41039, so some subjects moved."]),
        "I failed Programming 1")
    check("a good exchange reaches an answer", trace["stage"] == "answered")
    check("the engine result is carried through", trace["result"]["ok"])
    check("the reply is grounded", trace["grounding"]["grounded"])

    # A request whose intent and arguments are all resolved no longer reaches
    # the model's tool step at all, so the malformed-call check needs a request
    # that genuinely still has a choice in it. `list_plans` takes no resolved
    # argument, so it is one.
    browsing = ('{"institution":null,"plan_key":null,"plan_mention":null,'
                '"second_plan_mention":null,"failed_units":[],'
                '"completed_units":[],"intent":"list_plans",'
                '"unit_of_interest":null}')
    trace = L.answer(engine, L.StubBackend(
        [browsing, '{"tool":"drop_the_unit","args":{}}']), "drop it")
    check("a bad tool call stops before the engine", trace["stage"] == "tool_call_failed")
    check("no engine result is produced", "result" not in trace)

    # And the request that used to reach it now does not, because the dataset
    # has already settled every part of the call.
    trace = L.answer(engine, L.StubBackend(
        [PROFILE, '{"tool":"drop_the_unit","args":{}}',
         "You failed 41039, so some subjects moved."]), "I failed Programming 1")
    check("a settled request never asks the model which function to call",
          trace["tool_report"].get("resolved_without_model") is True)
    check("and the model's attempt to call something else is not reached",
          trace["tool_call"]["tool"] == "replan_after_failure")

    trace = L.answer(engine, L.StubBackend(["x", "y"]), "???")
    check("a failed intake stops before the tool step", trace["stage"] == "intake_failed")
    check("no tool call is attempted", "tool_call" not in trace)

    # The engine refusing is a real answer, not a crash, and must stay visible
    # as a refusal rather than being dressed up by the explanation step.
    trace = L.answer(engine, L.StubBackend([
        '{"institution":"UTS","plan_key":"%s","failed_units":["16138"],'
        '"completed_units":[],"intent":"replan_after_failure",'
        '"unit_of_interest":"16138"}' % PLAN,
        '{"tool":"replan_after_failure","args":{"institution":"UTS","key":"%s",'
        '"failed_unit":"16138"}}' % PLAN,
        "That subject is not in your plan."]), "I failed 16138")
    check("an engine refusal is reported as a refusal",
          trace["stage"] == "engine_refused")
    check("the refusal carries a reason", bool(trace["result"].get("error")))


def test_resolution():
    """Names the student wrote become codes here, or become a question."""
    print("\nresolution")
    engine = api.Engine()

    def profile(**over):
        base = {"institution": "UTS", "plan_key": None, "plan_mention": None,
                "failed_units": [], "completed_units": [], "intent": "unclear",
                "unit_of_interest": None}
        base.update(over)
        return base

    r, _ = L.resolve_profile(engine, profile(failed_units=["Programming 1"]))
    check("a unit name becomes its code", r["failed_units"] == ["41039"])

    r, _ = L.resolve_profile(engine, profile(failed_units=["41039"]))
    check("a code passes through", r["failed_units"] == ["41039"])

    r, _ = L.resolve_profile(engine, profile(plan_mention="my software degree"))
    check("a described degree becomes a plan key",
          r["plan_key"] == "Software Engineering major")

    _, rep = L.resolve_profile(engine, profile(plan_mention="civil"))
    check("an ambiguous degree is not guessed at", bool(rep["ambiguous"]))
    check("the ambiguity lists the candidates",
          len(rep["ambiguous"][0]["candidates"]) > 1)

    _, rep = L.resolve_profile(engine, profile(failed_units=["Programming"]))
    check("an ambiguous unit name is not guessed at", bool(rep["ambiguous"]))

    _, rep = L.resolve_profile(engine, profile(failed_units=["Quantum Basketweaving"]))
    check("an unknown unit is reported", bool(rep["unresolved"]))

    _, rep = L.resolve_profile(engine, profile(institution=None))
    check("a missing institution is reported",
          any(x["field"] == "institution" for x in rep["unresolved"]))

    # The exact failure seen on the real model: institution and intent right,
    # everything else empty. It must ask rather than look up a plan named None.
    trace = L.answer(engine, L.StubBackend([
        '{"institution":"UTS","plan_key":null,"plan_mention":null,'
        '"failed_units":[],"completed_units":[],'
        '"intent":"replan_after_failure","unit_of_interest":null}']),
        "I failed Programming 1 in my UTS software degree")
    check("an underspecified request asks instead of guessing",
          trace["stage"] == "needs_clarification")
    check("no tool call is made for it", "tool_call" not in trace)


# ------------------------------------------------------- persona independence

def test_persona_independence():
    """
    The repairs Section 4.5.5 makes to the stages that moved with the student.

    Each of these is a case the measurement actually produced, written as a test
    so that a later change cannot reintroduce it quietly.
    """
    print("\npersona independence")
    engine = api.Engine()

    def profile(**kw):
        base = {"institution": "UTS", "plan_key": None, "plan_mention": None,
                "second_plan_mention": None, "failed_units": [],
                "completed_units": [], "intent": "unclear",
                "unit_of_interest": None}
        base.update(kw)
        return base

    # The flip two personas in seven produced: the same message read as a
    # validity check rather than a repair.
    settled, override = L.settle_intent(
        profile(failed_units=["41039"], intent="validate_plan"))
    check("a stated failure makes it a repair, not a validity check",
          settled["intent"] == "replan_after_failure")
    check("the correction is recorded rather than hidden",
          override == {"from": "validate_plan", "to": "replan_after_failure"})

    settled, override = L.settle_intent(
        profile(failed_units=["41039"], intent="replan_after_failure"))
    check("an intent the model already had right is left alone", override is None)

    settled, _ = L.settle_intent(profile(intent="get_plan",
                                         plan_mention="Software Engineering major"))
    check("a plan request with no failure stays a plan request",
          settled["intent"] == "get_plan")

    settled, _ = L.settle_intent(profile(intent="get_plan",
                                         plan_mention="Civil at Sydney",
                                         second_plan_mention="Civil at UTS"))
    check("two degrees named makes it a comparison",
          settled["intent"] == "compare_degrees")

    settled, _ = L.settle_intent(profile(unit_of_interest="AMME3500",
                                         institution="USYD"))
    check("a bare unit question becomes a lookup",
          settled["intent"] == "lookup_unit")

    # The one persona in seven who wrote the university into the degree name and
    # was asked a clarifying question the other six were not.
    outcome = api.resolve_plan(engine, "UTS", "Civil Engineering major at UTS")
    check("a degree named with its university still resolves",
          outcome["ok"] and outcome["key"] == "Civil Engineering major")
    check("it resolves to the same plan as the bare name",
          outcome.get("key") == api.resolve_plan(
              engine, "UTS", "Civil Engineering major").get("key"))

    outcome = api.resolve_plan(engine, "UTS", "civil")
    check("trimming has not made a real ambiguity resolve",
          not outcome["ok"] and outcome.get("ambiguous"))

    # The comparison path, which had no resolution at all and refused every run.
    pair = L.resolve_comparison(engine, profile(
        institution=None, plan_mention="Software Engineering at Sydney",
        second_plan_mention="the Software Engineering major at UTS",
        intent="compare_degrees"))
    check("a comparison resolves both keys without the model", pair["ok"])
    check("the Sydney side is a Sydney plan",
          pair.get("usyd_key") in engine.plans["USYD"])
    check("the UTS side is a UTS plan", pair.get("uts_key") in engine.plans["UTS"])

    pair = L.resolve_comparison(engine, profile(
        institution=None, intent="compare_degrees"))
    check("a comparison naming no degree asks rather than guessing",
          not pair["ok"] and pair.get("candidates"))

    # The persona text must not reach the comparison, which is what Section 3.8
    # undertakes. A persona mentioning no degree cannot select one.
    pair = L.resolve_comparison(engine, profile(
        institution=None, intent="compare_degrees",
        plan_mention="I'm a mature age student with a mortgage and two kids"))
    check("a student's account of themselves selects no degree", not pair["ok"])

    # The whole exchange, run once per persona through a stub that returns the
    # same intake for each: the answer must be identical, and must not depend on
    # the model being asked a second question.
    prefixes = ["", "I came straight out of year 12 with a 98 ATAR. ",
                "I'm a mature age student with two kids. ",
                "Honestly I've been struggling and I'm not sure I'm cut out for this. "]
    landings = set()
    for prefix in prefixes:
        trace = L.answer(engine, L.StubBackend([
            '{"institution":null,"plan_key":null,'
            '"plan_mention":"Civil at Sydney",'
            '"second_plan_mention":"Civil at UTS","failed_units":[],'
            '"completed_units":[],"intent":"compare_degrees",'
            '"unit_of_interest":null}',
            "Sydney runs over 8 teaching periods and UTS over 8."]),
            prefix + "I'm deciding between Civil at Sydney and Civil at UTS.")
        landings.add(json.dumps(trace.get("tool_call"), sort_keys=True))
    check("four personas reach one comparison call", len(landings) == 1)
    check("the comparison was resolved in code, not chosen by the model",
          trace["tool_report"].get("resolved_without_model") is True)


# ------------------------------------------------------------- unit lookups

def test_unit_lookup():
    """
    A unit's rules are a property of the unit, not of the degree or of the
    conversation it is asked about in.

    Asked for AMME3500 during a conversation about a UTS degree, this layer used
    to answer with two questions the student had no reason to expect: which of
    48024, 41039, 16263 or 48023 they meant, none of which is a Sydney unit or
    resembles the code they typed, and which plan they were on, which does not
    change a unit's prerequisites.
    """
    print("\nunit lookups")
    engine = api.Engine()

    def profile(**kw):
        base = {"institution": "UTS", "plan_key": None, "plan_mention": None,
                "second_plan_mention": None, "failed_units": [],
                "completed_units": [], "intent": "lookup_unit",
                "unit_of_interest": None}
        base.update(kw)
        return base

    outcome = api.resolve_unit(engine, "UTS", "AMME3500")
    check("a code from the other university is not matched to names here",
          not outcome["ok"] and not outcome.get("candidates"))
    check("and the refusal says what is wrong with it",
          "not a UTS unit code" in (outcome.get("error") or ""))

    found, where = api.resolve_unit_anywhere(engine, "UTS", "AMME3500")
    check("the dataset that holds the code decides the university",
          found["ok"] and found["code"] == "AMME3500" and where == "USYD")

    found, where = api.resolve_unit_anywhere(engine, "USYD", "Programming 1")
    check("a name still resolves where it belongs",
          found["ok"] and found["code"] == "41039" and where == "UTS")

    found, where = api.resolve_unit_anywhere(engine, "UTS", "ZZZZ9999")
    check("a code in neither dataset resolves in neither",
          not found["ok"] and where == "UTS")

    r, report = L.resolve_profile(engine, profile(unit_of_interest="AMME3500"))
    check("a lookup is not asked which plan the student is on",
          not report["unresolved"] and not report["ambiguous"])
    check("and it is answered against Sydney's dataset",
          r["institution"] == "USYD" and r["unit_of_interest"] == "AMME3500")

    r, report = L.resolve_profile(engine, profile(institution=None,
                                                  unit_of_interest="33130"))
    check("a lookup with no university named finds the one that has the unit",
          r["institution"] == "UTS" and r["unit_of_interest"] == "33130")

    r, report = L.resolve_profile(engine, profile(unit_of_interest="ZZZZ9999"))
    check("an unknown code is reported rather than resolved",
          r["unit_of_interest"] is None and report["unresolved"])
    check("and the question asked back names the real problem",
          "not a UTS unit code" in L.describe_gaps(report))

    # The plan carried from an earlier question must not reach a lookup, since
    # it cannot change the answer and can only make it refusable.
    r, _ = L.resolve_profile(engine, profile(unit_of_interest="AMME3500"))
    check("no plan is resolved for a lookup", not r.get("plan_key"))

    # The whole exchange. Intake says UTS, because that is where the
    # conversation was, and the code says otherwise. The stub offers one reply
    # after intake: if the layer asked the model to choose the function, that
    # reply would be consumed as a tool call and there would be none left to
    # explain with, so this also shows the call was built in code.
    intake = json.dumps({"institution": "UTS", "plan_key": None,
                         "plan_mention": None, "second_plan_mention": None,
                         "failed_units": [], "completed_units": [],
                         "intent": "lookup_unit",
                         "unit_of_interest": "AMME3500"})
    trace = L.answer(engine,
                     L.StubBackend([intake, "AMME3500 requires AMME2500."]),
                     "What do I need before AMME3500?")
    check("a lookup reaches an answer, not a question", trace["stage"] == "answered")
    check("the call was built from the dataset, not chosen by the model",
          trace["tool_report"].get("resolved_without_model") is True)
    check("and it was sent to the university that has the unit",
          (trace["tool_call"] or {}).get("args", {}).get("institution") == "USYD")
    check("the engine answered about the unit asked for",
          trace["result"].get("ok") and trace["result"].get("unit") == "AMME3500")

    # AMME3500's prerequisite is both AMME2000 and AMME2500. The structured form
    # says so as [["AMME2000"], ["AMME2500"]], an AND of ORs, and the model read
    # that as "either AMME2000 or AMME2500", which is the opposite rule. The
    # result therefore carries the sentence as well, rendered by the rules object
    # that enforces it, and the explanation is shown the sentence and not the
    # groups.
    record = api.lookup_unit(engine, "USYD", "AMME3500")
    check("a conjunction is stated as a conjunction",
          record.get("prerequisite_in_words") == "(AMME2000) AND (AMME2500)")

    disjunction = api.lookup_unit(engine, "USYD", "AMME2000")
    check("and a disjunction as a disjunction",
          " or " in (disjunction.get("prerequisite_in_words") or "")
          and "AND" not in (disjunction.get("prerequisite_in_words") or ""))

    none = api.lookup_unit(engine, "UTS", "33130")
    check("nothing recorded is not reported as nothing required",
          none.get("prerequisite_in_words") == "none recorded")

    view = L.for_explanation(record)
    check("the explanation is shown the sentence, not the groups",
          "prerequisite_in_words" in view
          and "prerequisite_groups" not in view)
    check("and both codes are still there to be grounded against",
          L.check_grounding(
              "AMME3500 needs both AMME2000 and AMME2500.",
              record)["grounded"])


# --------------------------------------------------------- carried context

def test_carried_context():
    print("\ncarried context")

    def profile(**kw):
        base = {"institution": None, "plan_key": None, "plan_mention": None,
                "second_plan_mention": None, "failed_units": [],
                "completed_units": [], "intent": "unclear",
                "unit_of_interest": None}
        base.update(kw)
        return base

    comparison = profile(plan_mention="Civil at Sydney",
                         second_plan_mention="Civil at UTS",
                         intent="compare_degrees")

    p, taken = L.carry_context(profile(), dict(comparison))
    check("a follow-up to a comparison is still a comparison",
          p["intent"] == "compare_degrees" and "intent" in taken)
    check("both degrees are carried, not just the first",
          p["plan_mention"] and p["second_plan_mention"])

    p, taken = L.carry_context(profile(intent="get_plan",
                                       plan_mention="Mechatronic at Sydney"),
                               dict(comparison))
    check("a follow-up that names its own degree is left alone",
          p["intent"] == "get_plan" and p["plan_mention"] == "Mechatronic at Sydney"
          and not p["second_plan_mention"])

    p, _ = L.carry_context(profile(failed_units=["41039"],
                                    intent="replan_after_failure"),
                           dict(comparison))
    check("a failure after a comparison is a repair, not a comparison",
          p["intent"] == "replan_after_failure")

    p, _ = L.carry_context(profile(intent="lookup_unit",
                                    unit_of_interest="AMME3500"),
                           {"institution": "UTS", "plan_key": PLAN,
                            "intent": "get_plan"})
    check("a lookup carries no plan from the question before it",
          not p["plan_key"] and not p["plan_mention"])

    # Stepping out of a comparison onto one side of it. "Now just show me the
    # Sydney one" names a university, which is how a student says they want one
    # degree and not the pair, and it was being carried back into a comparison.
    SYD = "Software_stream_Computer_Engineering_breadth_Humanitarian_Engineering"
    paired = profile(plan_mention="Software Engineering at Sydney",
                     second_plan_mention="the Software Engineering major at UTS",
                     intent="compare_degrees",
                     compared_usyd_key=SYD,
                     compared_uts_key="Software Engineering major")

    p, _ = L.carry_context(profile(institution="USYD"), dict(paired))
    check("naming one side leaves the comparison behind",
          p["intent"] != "compare_degrees")
    check("and picks up that side's key exactly", p["plan_key"] == SYD)

    p, _ = L.carry_context(profile(institution="UTS"), dict(paired))
    check("the other side resolves to the other key",
          p["plan_key"] == "Software Engineering major")

    p, _ = L.carry_context(profile(), dict(paired))
    check("naming neither side is still a comparison",
          p["intent"] == "compare_degrees" and not p["plan_key"])

    p, _ = L.carry_context(profile(institution="USYD"), dict(paired))
    check("stepping onto one side settles the intent too",
          p["intent"] == "get_plan")

    p, _ = L.carry_context(profile(institution="UTS", failed_units=["41039"],
                                    intent="replan_after_failure"), dict(paired))
    check("a failure named on one side stays a repair",
          p["intent"] == "replan_after_failure"
          and p["plan_key"] == "Software Engineering major")

    # The pair has to survive more than one follow-up, or the second one loses
    # the comparison the first one was still inside.
    p, _ = L.carry_context(profile(), dict(paired))
    second, _ = L.carry_context(profile(institution="USYD"), dict(p))
    check("the pair survives a second follow-up", second["plan_key"] == SYD)

    # Three whole exchanges: onto one side, onto the other, and staying put.
    engine2 = api.Engine()
    ctx = dict(paired)

    def side_trace(institution, message):
        intake = json.dumps({"institution": institution, "plan_key": None,
                             "plan_mention": None, "second_plan_mention": None,
                             "failed_units": [], "completed_units": [],
                             "intent": "unclear", "unit_of_interest": None})
        # The model is made to push for a comparison, which is what it actually
        # did on this follow-up: a settled intent must not consult it at all.
        return L.answer(engine2, L.StubBackend([
            intake,
            '{"tool":"compare_degrees","args":{"usyd_key":"%s",'
            '"uts_key":"Software Engineering major"}}' % SYD,
            "prose"]), message, context=dict(ctx))

    trace = side_trace("UTS", "now just show me the UTS one")
    check("the UTS side is answered as a plan, not a comparison",
          (trace.get("tool_call") or {}).get("tool") == "get_plan")
    check("with the key the pairing chose",
          trace["tool_call"]["args"]["key"] == "Software Engineering major")

    trace = side_trace("USYD", "now just show me the Sydney one")
    check("and the Sydney side with its own key",
          trace["tool_call"]["args"]["key"] == SYD)
    check("the model's push for a comparison was never consulted",
          trace["tool_report"].get("resolved_without_model") is True)
    check("so the result is a plan and not a comparison",
          "schedule" in trace["result"] and "usyd" not in trace["result"])

    trace = side_trace(None, "how is the unit structure different?")
    check("naming neither side still compares",
          (trace.get("tool_call") or {}).get("tool") == "compare_degrees")


# --------------------------------------------------- the institution repair

def test_institution_repair():
    """
    Intake is told, with examples, that a university named on its own fills
    `institution`. On a short follow-up the 8B model still returns every field
    empty, and that one field is what separates "show me the Sydney one" from
    "how do they differ", so a model that forgets it answers a different
    question. The repair fills it when the message names one university and
    only one.
    """
    print("\nthe institution repair")

    def profile(**kw):
        base = {"institution": None, "plan_key": None, "plan_mention": None,
                "second_plan_mention": None, "failed_units": [],
                "completed_units": [], "intent": "unclear",
                "unit_of_interest": None}
        base.update(kw)
        return base

    cases = [
        ("now just show me the Sydney one", "USYD"),
        ("and the UTS one?", "UTS"),
        ("what about at USyd", "USYD"),
        # The full UTS name ends in the other university's name.
        ("the University of Technology Sydney version", "UTS"),
        ("at the University of Sydney", "USYD"),
        # Neither named, or both: there is nothing to settle, so nothing is.
        ("how is the unit structure different?", None),
        ("and the other one?", None),
        ("I'm deciding between Sydney and UTS", None),
    ]
    for message, expected in cases:
        filled, named = L.repair_institution(profile(), message)
        check(f"{message[:40]!r} -> {expected}",
              filled["institution"] == expected and named == expected)

    # It never overwrites what the model did extract.
    filled, named = L.repair_institution(profile(institution="UTS"),
                                         "show me the Sydney one")
    check("an institution the model did extract is left alone",
          filled["institution"] == "UTS" and named is None)

    # The thing Section 3.8 is protecting. A description of the student cannot
    # change what this does, because the only thing it can write is one of two
    # constants and the only thing it reads is which university was named.
    personas = ["I'm a mature age student with two kids and a mortgage. ",
                "I came straight out of year 12 with a 98 ATAR. ",
                "Honestly I've been struggling and I might drop out. ",
                ""]
    landings = {L.repair_institution(profile(), p + "show me the Sydney one")[1]
                for p in personas}
    check("no account of the student changes the repair", landings == {"USYD"})

    # It runs only inside a follow-up to a comparison, because on a first
    # request it cannot tell "the Sydney one" from "I moved to Sydney last
    # year", and a plan chosen from biography is the failure Section 3.8 names.
    # The gate is in the exchange rather than in the function, so it is checked
    # there, and it is recorded rather than applied quietly.
    engine = api.Engine()
    blank = json.dumps(profile())
    pair = {**profile(), "intent": "compare_degrees",
            "plan_mention": "Software at Sydney",
            "second_plan_mention": "the same major at UTS",
            "compared_usyd_key": "Software_stream_Computer_Engineering_"
                                 "breadth_Humanitarian_Engineering",
            "compared_uts_key": "Software Engineering major"}

    trace = L.answer(engine, L.StubBackend([blank, "prose"]),
                     "now just show me the Sydney one", context=dict(pair))
    check("the repair runs after a comparison",
          trace.get("institution_repaired") == "USYD")
    check("and is reported on the trace", "institution_repaired" in trace)

    trace = L.answer(engine, L.StubBackend([blank, "x", "prose"]),
                     "I moved to Sydney last year, show me a plan")
    check("it does not run on a first request",
          trace.get("institution_repaired") is None)

    trace = L.answer(engine, L.StubBackend([blank, "prose"]),
                     "how is the unit structure different?",
                     context=dict(pair))
    check("and reports nothing when it did nothing",
          trace.get("institution_repaired") is None)


# ------------------------------------------------- what reaches the engine

def test_argument_boundary():
    """
    The model picks the function. The dataset supplies the arguments.

    `choose_tool` checked the argument names against the signature and let any
    value through, so a message the intent rules read as "unclear" skipped the
    gate that asks for a missing degree, and the model could then call
    `get_plan` with a plan key of its own. That is the one thing Section 3.9
    says cannot happen, and it reached the engine and came back as a confident
    answer about a degree the student had not named.
    """
    print("\nwhat reaches the engine")
    engine = api.Engine()

    def profile(**kw):
        base = {"institution": "UTS", "plan_key": PLAN, "plan_mention": None,
                "second_plan_mention": None, "failed_units": [],
                "completed_units": [], "intent": "get_plan",
                "unit_of_interest": None}
        base.update(kw)
        return base

    other = "Civil Engineering major"
    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"get_plan","args":{"institution":"UTS",'
                       '"key":"%s"}}' % other]),
        profile())
    check("a key the model supplied is replaced by the resolved one",
          call and call["args"]["key"] == PLAN)
    check("and the substitution is recorded, not silent",
          report["overridden_args"].get("key", {}).get("model") == other)

    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"get_plan","args":{"institution":"USYD",'
                       '"key":"%s"}}' % PLAN]),
        profile())
    check("an institution the model supplied is replaced too",
          call and call["args"]["institution"] == "UTS")

    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"get_plan","args":{"institution":"UTS",'
                       '"key":"%s"}}' % PLAN]),
        profile())
    check("a call that already agrees records no override",
          call and not report["overridden_args"])

    # The case from the transcript: nothing resolved, and the model fills it in.
    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"get_plan","args":{"institution":"USYD",'
                       '"key":"Software_stream_Computer_Engineering_'
                       'breadth_Humanitarian_Engineering"}}']),
        profile(institution="USYD", plan_key=None, intent="unclear"))
    check("a key the dataset never resolved does not reach the engine",
          call is None)
    check("and the refusal says which argument was missing",
          any("needs key" in e for e in report["errors"]))

    call, report = L.choose_tool(
        L.StubBackend(['{"tool":"replan_after_failure","args":'
                       '{"institution":"UTS","key":"%s","failed_unit":"99999"}}'
                       % PLAN]),
        profile(failed_units=["41039"], intent="replan_after_failure"))
    check("an invented failed unit is replaced by the resolved one",
          call and call["args"]["failed_unit"] == "41039")

    # A whole exchange that used to answer and should now ask.
    intake = json.dumps({"institution": "USYD", "plan_key": None,
                         "plan_mention": None, "second_plan_mention": None,
                         "failed_units": [], "completed_units": [],
                         "intent": "unclear", "unit_of_interest": None})
    trace = L.answer(engine, L.StubBackend([
        intake,
        '{"tool":"get_plan","args":{"institution":"USYD","key":"Software_stream_'
        'Computer_Engineering_breadth_Humanitarian_Engineering"}}',
        "prose"]), "now just show me the Sydney one")
    check("the exchange asks instead of answering",
          trace["stage"] == "needs_clarification")
    check("no engine result was produced", trace.get("result") is None)
    check("and the question names what is missing",
          "degree and plan" in trace["reply"])


# ------------------------------------------------------- plan descriptions

def test_plan_descriptions():
    """
    A degree named with its specialisation must resolve to that plan.

    Three Sydney Mechatronic plans share a degree name and are told apart only by
    the specialisation, so a student who names theirs has given enough and should
    not be asked again. The words joining the two together are the student's
    phrasing rather than part of either name.
    """
    print("\nplan descriptions")
    engine = api.Engine()

    phrases = [
        "Mechatronic with the Robotics and Intelligent Systems specialisation",
        "Mechatronic at Sydney with the Robotics and Intelligent Systems "
        "specialisation",
        "Mechatronic Robotics and Intelligent Systems",
    ]
    keys = set()
    for phrase in phrases:
        outcome = api.resolve_plan(engine, "USYD", phrase)
        check(f"resolves: {phrase[:48]}", outcome["ok"],
              outcome.get("error", ""))
        if outcome["ok"]:
            keys.add(outcome["key"])
    check("every phrasing reaches the same plan", len(keys) == 1)

    for vague in ("Mechatronic", "civil", "my software degree"):
        outcome = api.resolve_plan(engine, "USYD", vague)
        check(f"still ambiguous, correctly: {vague!r}",
              not outcome["ok"] and outcome.get("ambiguous"))


if __name__ == "__main__":
    test_intake()
    test_tool_calls()
    test_resolution()
    test_unit_lookup()
    test_carried_context()
    test_institution_repair()
    test_argument_boundary()
    test_plan_descriptions()
    test_grounding()
    test_exchange()
    test_persona_independence()
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)
