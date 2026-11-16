"""
Path A: a descriptive comparison of two engineering degrees (Section 3.8).

The comparison answers the questions a student actually asks before enrolling:
how long is it, how much does it cost, how much choice do I get, what can I
specialise in, how many subjects will I carry a session. It deliberately does
not measure prerequisite structure. That needs requisite coverage the Sydney
dataset does not yet have (Section 4.2.4), and a structural complexity figure
computed over units with no recorded requisites would describe the dataset
rather than the degree.

Everything here is read from the dataset or from `degree_facts.json`, which
records the administrative figures with the page each came from. The language
model is given the result to explain and never produces any part of it, which is
what makes the output checkable against its sources.
"""

import json
import os
import re
from collections import Counter

import rules as R
from paths import P

FACTS_FILE = P("degree_facts.json")

# UTS names each major after its Sydney counterpart, which is the pairing this
# comparison uses. Aeronautical has no UTS major, so it has no pair.
UNPAIRED_USYD = ("Aeronautical",)


def load_facts(root="."):
    with open(os.path.join(root, FACTS_FILE), encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ structure

def _specialisation_name(block_name):
    """Collapse a block name to the specialisation it belongs to."""
    s = block_name.replace("Specialisation - ", "")
    s = re.sub(r" (Core|Elective|Selective|Breadth) Units$", "", s)
    s = re.sub(r" Units$", "", s)
    return s.strip()


def usyd_specialisations(engine, degree):
    """Named specialisations a Sydney degree offers, deduplicated."""
    blocks = engine.data["USYD"][degree]["blocks"]
    return sorted({_specialisation_name(b) for b in blocks
                   if b.startswith("Specialisation - ")})


# The university-wide elective allowance. Offered by most majors, it is not a
# specialisation the major names: it is 24 credit points the student may spend
# anywhere in a pool drawn from the whole university.
UNIVERSITY_WIDE_ELECTIVES = "CBK90011"


def _block_title(blocks, code):
    """A block's own name, with the first subject it lists trimmed off."""
    name = blocks.get(code, {}).get("name") or ""
    if not name:
        return None
    # The scraper concatenates a block's name with its first subject, as in
    # "Control Studios stream 48560 Control Studio A". Cut at the subject code.
    return re.split(r"\s+\d{4,6}\s", name)[0].strip() or None


def uts_specialisations(engine, major):
    """
    Specialisations UTS names inside a major.

    Read from the major's own suggested sequence rather than from block names,
    because the names do not say which major a block belongs to: Electrical's
    streams are called "Control Studios" and "Power Systems Studios", with no
    mention of Electrical anywhere in them.

    A choice entry in the sequence offers blocks. A sub-major block is itself a
    specialisation; a choice block belonging to the major is a container, and its
    sub-blocks are the specialisations. The university-wide elective allowance is
    neither, and is reported separately.

    An empty result is a finding rather than a gap: for those majors UTS names no
    specialisation at all, which is what Section 4.6 reports.
    """
    blocks = engine.data["UTS"]["blocks"]
    sequence = (engine.data["UTS"].get("sequences") or {}).get(major) or []

    offered, wide = set(), False
    for entry in sequence:
        if entry.get("kind") != "choice":
            continue
        for option in entry.get("options") or []:
            if not option.get("is_block"):
                continue
            code = option["code"]
            if code == UNIVERSITY_WIDE_ELECTIVES:
                wide = True
            elif code.startswith("SMJ"):
                offered.add(option.get("name") or code)
            elif code.startswith("CBK"):
                # A container belonging to this major. Its sub-blocks are the
                # specialisations; a block with units and no sub-blocks is a
                # subject choice, not a specialisation.
                for sub in blocks.get(code, {}).get("sub_blocks") or []:
                    offered.add(_block_title(blocks, sub) or sub)

    return sorted(offered), wide


def uts_elective_pool(engine):
    """How many sub-majors the unrestricted elective allowance can be spent on."""
    block = engine.data["UTS"]["blocks"].get(UNIVERSITY_WIDE_ELECTIVES, {})
    return len(block.get("sub_blocks") or [])


def uts_published_specialisation_sequences(engine, major):
    """Specialisations UTS publishes as whole sequences of their own."""
    stem = major.split(",")[0]
    return sorted(k for k in engine.plans["UTS"]
                  if k.startswith(stem + ",") and k != major)


def _elective_range(engine, institution, plan):
    """[min, max] free-choice credit points over a degree's combinations."""
    if institution != "USYD":
        return None
    rules = engine.rules["USYD"]
    totals = []
    for other in engine.plans["USYD"].values():
        if other["degree"] != plan["degree"]:
            continue
        schedule = engine._schedule_for("USYD", other)
        slots = sum(1 for v in schedule.values() for c in v if R.is_placeholder(c))
        totals.append(slots * rules.elective_slot_cp)
    return [min(totals), max(totals)] if totals else None


def credit_structure(engine, institution, key):
    """Where the degree's credit points go."""
    plan = engine.plans[institution][key]
    if institution == "UTS":
        structure = dict(engine.data["UTS"].get("structure") or {})
        return {"core": structure.get("core"),
                "major": structure.get("major"),
                "electives_or_specialist_stream":
                    structure.get("electives_or_specialist_stream"),
                "professional_practice": structure.get("professional_practice"),
                "published_by_the_institution": True}

    # Sydney does not publish a core/major split. It is recovered from what each
    # placement counts toward, which is how the enrolment plans record it.
    buckets = Counter()
    for placement in plan["placements"]:
        toward = placement.get("counts_toward") or "unassigned"
        cp = placement.get("credit_points", 0)
        if toward == "base_plan_core":
            buckets["core_and_major_combined"] += cp
        elif toward == "unresolved":
            buckets["electives"] += cp
        else:
            buckets["specialisation"] += cp
    out = dict(buckets)
    out["published_by_the_institution"] = False
    out["note"] = ("Sydney publishes no split between a degree core and a major, "
                   "so the two are reported together.")
    return out


# ------------------------------------------------------------------- the money

def tuition(facts, institution, total_cp):
    """
    Tuition on a common basis.

    The institutions publish on bases that cannot be compared as published:
    Sydney per year at a 48 credit point load, UTS per session. Both are
    converted to a cost for the whole degree and a cost per credit point, which
    is the only form in which a 192 credit point degree and a 198 credit point
    one can be set beside each other.
    """
    out = {}

    dom = facts["domestic_commonwealth_supported"]
    eftsl = total_cp / dom["credit_points_per_eftsl"]
    out["domestic_commonwealth_supported"] = {
        "per_eftsl": dom["amount"],
        "eftsl_in_the_degree": round(eftsl, 3),
        "whole_degree": round(dom["amount"] * eftsl, 2),
        "per_credit_point": round(dom["amount"] / dom["credit_points_per_eftsl"], 2),
        "year": dom["year"],
        "set_nationally": dom["set_nationally"],
        "source": dom["source"]}

    intl = facts[institution]["international_tuition"]
    if intl["basis"] == "per year":
        per_cp = intl["amount"] / intl["credit_points_per_year"]
        whole = per_cp * total_cp
    elif intl["basis"] == "per session":
        whole = intl["amount"] * intl["sessions_in_course"]
        per_cp = whole / total_cp
    else:
        return out
    out["international"] = {
        "as_published": f"{intl['amount']:,} {intl['currency']} {intl['basis']}",
        "whole_degree": round(whole, 2),
        "per_credit_point": round(per_cp, 2),
        "year": intl["year"],
        "indicative": intl.get("indicative", False),
        "source": intl["source"]}
    return out


# ----------------------------------------------------------------- comparison

def describe(engine, institution, key, facts):
    """One side of the comparison."""
    import api

    plan = engine.plans[institution][key]
    rules = engine.rules[institution]
    data = engine._dataset_for(institution, plan)
    schedule = engine._schedule_for(institution, plan)

    teaching = [i for i in sorted(schedule)
                if schedule[i] and i % R.POSITIONS_PER_YEAR in (0, 2)]
    loads = [sum(rules.unit_cp(c, data) for c in schedule[i]) for i in teaching]
    counts = [len(schedule[i]) for i in teaching]
    placed = [c for v in schedule.values() for c in v if not R.is_placeholder(c)]
    open_slots = sum(1 for v in schedule.values() for c in v if R.is_placeholder(c))
    total_cp = sum(sum(rules.unit_cp(c, data) for c in v) for v in schedule.values())

    extra = {}
    if institution == "USYD":
        degree = plan["degree"]
        specs = usyd_specialisations(engine, degree)
        spec_note = None
        award = f"{facts['USYD']['award']} ({degree})"
        # A Sydney student picks a combination, so the number of combinations the
        # degree permits is the figure that matters, not the one plan shown here.
        extra["plans_in_the_dataset"] = sum(
            1 for p in engine.plans["USYD"].values() if p["degree"] == degree)
        extra["combination_shown"] = plan["title"]
    else:
        specs, wide = uts_specialisations(engine, key)
        published = uts_published_specialisation_sequences(engine, key)
        award = f"{facts['UTS']['award']} ({key.replace(' major', '')})"
        notes = []
        if not specs:
            notes.append("UTS names no specialisation inside this major.")
        if wide:
            notes.append("The 24 credit point elective allowance may be spent on "
                         f"any of {uts_elective_pool(engine)} sub-majors drawn from "
                         "a university-wide pool, with nothing in the major "
                         "indicating which are intended.")
        if published:
            notes.append("UTS additionally publishes "
                         f"{len(published)} specialisation(s) of this major as full "
                         "sequences of their own: "
                         + ", ".join(p.split(", ", 1)[1] for p in published) + ".")
        spec_note = " ".join(notes) or None
        extra["published_as_separate_sequences"] = published

    return {
        "institution": institution,
        "award": award,
        "key": key,
        "course_code": facts.get(institution, {}).get("course_code"),
        "length": {
            "nominal_years": facts[institution]["nominal_years"],
            "teaching_periods": len(teaching),
            "period_name": "semesters" if institution == "USYD" else "sessions"},
        "total_credit_points": total_cp,
        "credit_structure": credit_structure(engine, institution, key),
        "load": {
            "standard_period_credit_points": rules.min_cp_per_period,
            "maximum_period_credit_points": rules.max_cp_per_period,
            "mean_credit_points_per_period": round(sum(loads) / len(loads), 2),
            "periods_above_the_standard_load": sum(1 for l in loads
                                                   if l > rules.min_cp_per_period),
            "units_per_period_typical": round(sum(counts) / len(counts), 2),
            "units_per_period_range": [min(counts), max(counts)]},
        "choice": {
            "units_named_in_the_plan": len(placed),
            "slots_left_to_the_student": open_slots,
            "elective_credit_points": open_slots * rules.elective_slot_cp,
            # How much free choice a Sydney student has depends on which
            # specialisation combination they pick, so one plan's figure is not
            # the degree's. The range over every combination the dataset holds is.
            "elective_credit_points_across_combinations":
                _elective_range(engine, institution, plan)},
        "specialisations": {
            "count": len(specs),
            "names": specs,
            "note": spec_note,
            **extra},
        "tuition": tuition(facts, institution, total_cp)}


def compare(engine, usyd_key, uts_key, facts=None, root="."):
    facts = facts or load_facts(root)
    left = describe(engine, "USYD", usyd_key, facts)
    right = describe(engine, "UTS", uts_key, facts)

    diff = {
        "credit_point_difference": right["total_credit_points"] - left["total_credit_points"],
        "domestic_cost_difference": round(
            right["tuition"]["domestic_commonwealth_supported"]["whole_degree"]
            - left["tuition"]["domestic_commonwealth_supported"]["whole_degree"], 2),
        "specialisation_difference": (right["specialisations"]["count"]
                                      - left["specialisations"]["count"]),
        "elective_credit_point_difference": (right["choice"]["elective_credit_points"]
                                             - left["choice"]["elective_credit_points"])}

    return {"ok": True, "usyd": left, "uts": right, "difference": diff,
            "fee_year_caveat": facts["known_gaps"][0],
            "note": ("Descriptive and administrative comparison. Says nothing about "
                     "teaching quality, workload or career outcomes, and does not "
                     "measure prerequisite structure.")}


def pairs(engine):
    """The Sydney degree and UTS major pairings, from the dataset's own map."""
    mapping = engine.data["UTS"].get("major_map_to_usyd") or {}
    out = []
    for uts_major, usyd_degree in sorted(mapping.items(), key=lambda kv: kv[1]):
        degree = usyd_degree.replace(" Engineering", "")
        keys = [k for k in engine.plans["USYD"]
                if engine.plans["USYD"][k]["degree"] == degree]
        if keys and uts_major in engine.plans["UTS"]:
            out.append((sorted(keys)[0], uts_major))
    return out
