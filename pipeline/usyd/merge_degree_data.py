"""
Merge the two scraped USYD Software Engineering data sources into one file.

  - stream_table_units.json (from scale_live_fetch.py): the Software
    Stream's own unit-of-study table, with prerequisite/corequisite/
    prohibition/assumed-knowledge data parsed from the requirements
    column. Only covers the Software Stream Core + Electives - it doesn't
    know about Introductory Computing, Foundation Maths, Projects, PEP, or
    Free Electives, since those aren't part of the stream table page.

  - cusp_degree_structure.json (from scrape_cusp_degree.py): every
    requirement block for the full degree (all 20 blocks), plus the
    official Year 1 Sem 1 -> Year 4 Sem 2 enrolment structure. Doesn't have
    prerequisite/corequisite data, since CUSP's degree page doesn't list
    that (it's only on each unit's individual handbook page).

Output: merged_degree_data.json - one record per unique unit code,
combining whatever each source knows about it, plus which degree
block(s) it belongs to (a unit can legitimately appear in more than one,
e.g. a specialisation elective that's also a general Stream elective).

Units that only appear in cusp_degree_structure.json (e.g. INFO1110,
MATH1061, ENGP1001) get prerequisite_codes etc. set to None rather than
[] - that distinguishes "confirmed no prerequisites" from "we have no
prerequisite data for this unit at all", which matters for the planner:
a None should trigger a live handbook-page check before the planner
assumes a unit is prereq-free.

MANUAL OVERRIDES: ELEC4712 (Thesis A) and ELEC4713 (Thesis B) are
referenced by code in CUSP's Year 4 semester_plan entries, but never
appear as actual rows in any CUSP unit_blocks table - the Projects Table
block only lists generic project units shared across other engineering
streams (BMET4111, ELEC1005, etc.), and mentions stream-specific thesis
units like these only in a footnote ("Students in both Electrical and
Software are to enrol in the ELEC coded units"). That meant they were
silently missing from merged_degree_data.json entirely - not just a
cosmetic "Unknown unit" display issue, but a real 0cp-instead-of-6cp bug
in every semester-load calculation involving Year 4. Added below with
data confirmed directly against sydney.edu.au/units/ELEC4712 and
.../ELEC4713 (fetched 2026-08-11).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import json
import re

STREAM_TABLE_FILE = P("stream_table_units.json")
CUSP_STRUCTURE_FILE = P("cusp_degree_structure.json")
UNIT_SESSIONS_FILE = P("unit_sessions.json")
OUTPUT_FILE = P("merged_degree_data.json")

# All 7 Table S faculty files from scrape_all_table_s.py. Confirmed
# 2026-08-27: these were scraped correctly (e.g. SIEN2001's real
# prerequisite "SIEN1000 and SIEN1001" IS present in
# table_s_business_school_units.json) but merge_degree_data.py never
# actually loaded any of them - only STREAM_TABLE_FILE. That meant every
# cross-faculty specialisation unit (Innovation and Entrepreneurship's
# SIEN/MKTG/DECO units, and presumably Humanitarian Engineering's or any
# other breadth specialisation's non-Engineering units) fell through to
# prerequisite_codes=None even though the real data was sitting right
# there in a file this script just never opened. Found while manually
# verifying SIEN2001/DECO2015/MKTG3114/SIEN3001 against live handbook
# pages one at a time and discovering the merged output didn't match any
# of them - checking the raw scrape files directly (rather than assuming
# the scrape itself had failed) showed the data was correct all along.
TABLE_S_FILES = [
    P("table_s_engineering_units.json"),
    P("table_s_science_units.json"),
    P("table_s_business_school_units.json"),
    P("table_s_architecture_design_planning_units.json"),
    P("table_s_arts_units.json"),
    P("table_s_medicine_health_units.json"),
    P("table_s_conservatorium_units.json"),
]

# Structured representation of a "N credit points of [prefix][level range]
# units [except (...)]" prerequisite - the kind that can't be expressed as
# a specific list of alternative codes (see MATH2069/ELEC4712 below).
# min_level/max_level bound the unit's numeric level (e.g. COMP3027 is
# level 3000); max_level=None means "or higher" (unbounded). prefix
# restricts to codes starting with that string (None = any prefix, e.g.
# ELEC4712's "BE units" spans the whole engineering degree, not one
# subject code). excluded_patterns are regexes - a unit matching any of
# them does NOT count even if it's otherwise in range (MATH2069 excludes
# both a wildcard-shaped group, MATH1XX5, and two specific codes).
#
# ELEC4712's prerequisite ("36 credit points of 3000- or higher level BE
# units of study") is confirmed directly against
# sydney.edu.au/units/ELEC4712 (fetched 2026-08-11) and included in its
# manual override below. MATH2069's ("6 credit points of MATH1XXX except
# (MATH1XX5 or MATH1050 or MATH1111)") is patched in separately since
# MATH2069 itself comes from the real stream-table scrape, not a manual
# override - only this structured requirement needs adding on top.
CREDIT_REQUIREMENT_PATCHES = {
    "MATH2069": {
        "min_credit_points": 6,
        "min_level": 1000,
        "max_level": 1999,
        "prefix": "MATH",
        "excluded_patterns": [r"^MATH1\d\d5$", r"^MATH1050$", r"^MATH1111$"],
    },
}

# PEP (Professional Engagement Program) progression: ENGP1001 -> ENGP1002
# -> ... -> ENGP3002 must be taken in this exact order, one per semester,
# with NO gap - not just "sometime after", which an ordinary prerequisite
# would allow. Confirmed directly against individual PEP handbook pages
# (fetched 2026-08-11), e.g. ENGP1003: "Students must enrol in ENGP1003 in
# the semester immediately following completion of ENGP1002", and
# ENGP3001: "...immediately following completion of ENGP2003." Each
# successor gets BOTH an ordinary prerequisite_codes entry (so it's
# treated as needing its predecessor scheduled earlier, and so
# dependents/cascade detection picks it up like any other prerequisite)
# AND the stricter immediate_successor_of field, which
# curriculum_planner.py enforces as "exactly the next semester", not just
# "any earlier semester". Without this, nothing stopped the planner's
# compaction pass from freely piling multiple PEP units into one
# semester, since they otherwise carry no prerequisite data at all.
PEP_CHAIN = [
    "ENGP1001", "ENGP1002", "ENGP1003",
    "ENGP2001", "ENGP2002", "ENGP2003",
    "ENGP3001", "ENGP3002",
]

# ELEC4712's prerequisite ("36 credit points of 3000- or higher level BE
# units of study") is a generic credit-point-threshold requirement, not
# expressible as specific unit codes - prerequisite_codes is correctly
# left empty (no false alternatives to satisfy), and the real requirement
# is captured properly in credit_point_requirement below, which the
# planner actually validates against (not just documents). ELEC4713's
# prerequisite, by contrast, genuinely is a specific code (ELEC4712) and
# is validated normally via prerequisite_codes.
MANUAL_UNIT_OVERRIDES = {
    "ELEC4712": {
        "code": "ELEC4712",
        "name": "Thesis A",
        "credit_points": 6,
        "sessions_offered": ["Semester 1", "Semester 2"],
        "degree_blocks": ["Projects Table"],
        "requirements_raw": "P 36 credit points of 3000- or higher level BE units of study N ELEC4714",
        "prerequisite_codes": [],
        "corequisite_codes": [],
        "prohibition_codes": ["ELEC4714"],
        "assumed_knowledge_codes": [],
        "discontinued": False,
        "credit_point_requirement": {
            "min_credit_points": 36,
            "min_level": 3000,
            "max_level": None,
            "prefix": None,
            "excluded_patterns": [],
        },
    },
    "ELEC4713": {
        "code": "ELEC4713",
        "name": "Thesis B",
        "credit_points": 6,
        "sessions_offered": ["Semester 1", "Semester 2"],
        "degree_blocks": ["Projects Table"],
        "requirements_raw": "P ELEC4712 N ELEC4714",
        "prerequisite_codes": ["ELEC4712"],
        "corequisite_codes": [],
        "prohibition_codes": ["ELEC4714"],
        "assumed_knowledge_codes": [],
        "discontinued": False,
    },
}

# Session-data-only override (not a full replacement like the two above -
# applied AFTER the CUSP seed, patching just sessions_offered): CUSP's own
# listing for SIEN2001 says Semester 2 only, but the live 2026 unit page
# (sydney.edu.au/units/SIEN2001, fetched 2026-08-27) confirms it actually
# runs in BOTH Semester 1 2026 and Semester 2 2026 - CUSP is stale here,
# not our scraper. Found while manually verifying this specific unit
# against the live handbook after it kept producing an impossible-looking
# scheduling conflict. Worth spot-checking other cross-faculty
# specialisation units' session data the same way if a similar conflict
# shows up again - CUSP's own per-unit session listing isn't guaranteed
# current for units outside the Faculty of Engineering.
SESSION_OVERRIDES = {
    "SIEN2001": ["Semester 1", "Semester 2"],
}


def load_unit_sessions():
    """
    Real, current session data fetched directly from each unit's own
    page by scrape_unit_sessions.py - the authoritative source for
    sessions_offered, confirmed more current than CUSP's degree-structure
    page (see that script's own docstring for the two confirmed failure
    modes: stale session listings, and discontinued units with no
    warning at all). Returns {} if the file doesn't exist yet - callers
    fall back to CUSP's data plus the hand-typed SESSION_OVERRIDES below
    for the handful of units already confirmed by hand.
    """
    try:
        with open(UNIT_SESSIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_stream_table():
    with open(STREAM_TABLE_FILE) as f:
        data = json.load(f)
    # stream_table_units.json may be an older plain list (pre-stream_info
    # wrapper) or the current {"stream_info": ..., "units": [...]} shape -
    # chat_query.py already handles both for the same reason; match that.
    if isinstance(data, list):
        stream_info = None
        units = data
    else:
        stream_info = data.get("stream_info")
        units = data.get("units", [])
    return {u["code"]: u for u in units}, stream_info


def load_cusp_structure():
    with open(CUSP_STRUCTURE_FILE) as f:
        return json.load(f)


def load_unit_sessions():
    """
    Live, per-unit session data from fetch_unit_sessions.py - preferred
    over CUSP's sessions_offered wherever available, since CUSP has been
    confirmed stale for at least one unit (SIEN2001) and doesn't reliably
    flag discontinued units at all (COMP3109, ELEC3702, ELEC3802 all
    still show as ordinary rows on the CUSP page - see that script's
    docstring). Returns {} if the file doesn't exist yet - this makes the
    live-session step optional so merge_degree_data.py still works before
    fetch_unit_sessions.py has been run for the first time, at the cost
    of falling back to CUSP's data for everything in that case.
    """
    try:
        with open(UNIT_SESSIONS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def load_table_s_units():
    """
    Combines every Table S faculty file into one code -> unit dict. Table
    S files have no sessions_offered field at all (that page is a big
    cross-unit requirements table, not a per-unit availability page) -
    only prerequisite/corequisite/prohibition/assumed-knowledge data.
    Later files in TABLE_S_FILES win on a duplicate code, but this
    shouldn't happen in practice - each unit's home faculty page is the
    one that lists it.
    """
    combined = {}
    for filename in TABLE_S_FILES:
        try:
            with open(filename) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        for u in data.get("units", []):
            combined[u["code"]] = u
    return combined


def expand_wildcard_code(pattern, known_codes):
    """
    Expand a wildcard-shaped token like "MATH1X61" (a stem notation
    standing for a whole family of real codes, e.g. MATH1061/MATH1961 -
    not itself a real unit code) into the real unit codes it stands for,
    checked against `known_codes` - the full set of codes actually in
    this degree's merged dataset. Substitutes every digit position
    independently (handles more than one X, e.g. "MATH19XX"), keeps only
    candidates that are real known codes, and returns them sorted.
    Returns [] if nothing matches - that's a normal, expected outcome
    (the alternative may genuinely not apply to this degree), not an
    error.
    """
    prefix, digits = pattern[:4], pattern[4:]
    x_positions = [i for i, ch in enumerate(digits) if ch == "X"]
    candidates = {digits}
    for pos in x_positions:
        next_candidates = set()
        for cand in candidates:
            for d in "0123456789":
                next_candidates.add(cand[:pos] + d + cand[pos + 1:])
        candidates = next_candidates
    return sorted(prefix + c for c in candidates if (prefix + c) in known_codes)


# Direct, hand-verified fix for DATA2002 and STAT2011, confirmed against
# the real handbook prerequisite text (2026-08-24) rather than trusting
# a generic wildcard-expansion pass end-to-end, since it has two
# confirmed gaps that both apply to these two units specifically:
#
#   1. Wildcard codes (MATH1X61, MATH1X62, etc.) appear as plain text,
#      not hyperlinks, in the stream table page - the scraper only reads
#      <a> tags, so these were silently dropped entirely rather than
#      expanded.
#   2. DATA2002's real structure is "(flat OR of 7 alternatives) OR
#      (MATH1X05 AND (OR of 8 alternatives))" - a nested AND inside one
#      branch of an OR, which the parser's flat AND-grouping can't
#      represent correctly (it flattens this into a rigid two-group AND,
#      a materially stricter requirement than the real one).
#
# Both units, once correctly read, are already satisfied by every
# Software Engineering student: DATA2002 by MATH1062 alone (one of its
# flat alternatives - the nested AND-branch is unreachable for a SWE
# student and correctly omitted below, since MATH1062 already satisfies
# the requirement outright); STAT2011 by MATH1061 (group 1) and MATH1062
# (group 2) together - both are mandatory Foundation Mathematics units
# every SWE student takes in Year 1. Alternatives that don't resolve to
# a real code in this degree (e.g. DATA1X01, MATH1X21, MATH1X01,
# MATH10X5 - none of which are Software Engineering units) are correctly
# omitted, not overlooked - checked directly via expand_wildcard_code().
PREREQUISITE_GROUP_OVERRIDES = {
    "DATA2002": {
        "prerequisite_codes": [
            "ENVX1002", "BUSS1020", "ECMT1010",
            "MATH1062", "MATH1962", "MATH1972",
        ],
        "prerequisite_groups": [
            ["ENVX1002", "BUSS1020", "ECMT1010", "MATH1062", "MATH1962", "MATH1972"],
        ],
    },
    "STAT2011": {
        "prerequisite_codes": [
            "MATH1061", "MATH1961", "MATH1971", "MATH1931", "MATH1906", "MATH1011",
            "MATH1062", "MATH1962", "MATH1972", "MATH1905", "STAT1021", "ECMT1010", "BUSS1020",
        ],
        "prerequisite_groups": [
            ["MATH1061", "MATH1961", "MATH1971", "MATH1931", "MATH1906", "MATH1011"],
            ["MATH1062", "MATH1962", "MATH1972", "MATH1905", "STAT1021", "ECMT1010", "BUSS1020"],
        ],
    },
}


def main():
    try:
        stream_units, stream_info = load_stream_table()
    except FileNotFoundError:
        print(f"[ERROR] {STREAM_TABLE_FILE} not found - run scale_live_fetch.py first.")
        return
    try:
        cusp_data = load_cusp_structure()
    except FileNotFoundError:
        print(f"[ERROR] {CUSP_STRUCTURE_FILE} not found - run scrape_cusp_degree.py first.")
        return
    table_s_units = load_table_s_units()
    unit_sessions = load_unit_sessions()

    merged = {}

    # Seed every unit from the CUSP blocks first, since that's the
    # complete set of required units across the whole degree.
    for block_name, block in cusp_data["unit_blocks"].items():
        for u in block["units"]:
            code = u["code"]
            if code not in merged:
                merged[code] = {
                    "code": code,
                    "name": u["name"],
                    "credit_points": u["credit_points"],
                    "sessions_offered": u["sessions_offered"],
                    "degree_blocks": [],
                    "prerequisite_codes": None,
                    "prerequisite_groups": [],
                    "corequisite_codes": None,
                    "corequisite_groups": [],
                    "prohibition_codes": None,
                    "assumed_knowledge_codes": None,
                    "discontinued": False,
                }
            merged[code]["degree_blocks"].append(block_name)

    # Apply confirmed session-data corrections where CUSP's own listing
    # is known to be stale (see SESSION_OVERRIDES above) - only takes
    # effect for a code unit_sessions.json doesn't already cover, since
    # live per-unit data below is strictly more current when present.
    for code, sessions in SESSION_OVERRIDES.items():
        if code in merged and code not in unit_sessions:
            merged[code]["sessions_offered"] = sessions

    # Live per-unit session data (fetch_unit_sessions.py) is the most
    # authoritative source available - each unit's OWN page, not CUSP's
    # degree-structure listing of it. Overrides sessions_offered
    # wherever fetched, and records which units are actually
    # discontinued (CUSP doesn't reliably flag this - see that script's
    # docstring for the confirmed COMP3109/ELEC3702/ELEC3802 cases).
    session_corrections = []
    discontinued_units = []
    for code, info in unit_sessions.items():
        if code not in merged:
            continue
        if info.get("discontinued"):
            merged[code]["discontinued"] = True
            discontinued_units.append(code)
        elif info.get("sessions_offered"):
            if set(info["sessions_offered"]) != set(merged[code]["sessions_offered"] or []):
                session_corrections.append(
                    (code, merged[code]["sessions_offered"], info["sessions_offered"])
                )
            merged[code]["sessions_offered"] = info["sessions_offered"]

    # Layer in prerequisite/corequisite/prohibition data from the stream
    # table scrape wherever we have it.
    matched = 0
    unmatched = []
    for code, su in stream_units.items():
        if code in merged:
            merged[code]["prerequisite_codes"] = su.get("prerequisite_codes", [])
            merged[code]["prerequisite_groups"] = su.get("prerequisite_groups", [])
            merged[code]["corequisite_codes"] = su.get("corequisite_codes", [])
            merged[code]["corequisite_groups"] = su.get("corequisite_groups", [])
            merged[code]["prohibition_codes"] = su.get("prohibition_codes", [])
            merged[code]["assumed_knowledge_codes"] = su.get("assumed_knowledge_codes", [])
            matched += 1
        else:
            # In the stream table but not in any CUSP block - shouldn't
            # normally happen, but worth surfacing rather than silently
            # dropping it if it does (e.g. a unit code mismatch).
            unmatched.append(code)

    # Fall back to Table S data for anything the stream table doesn't
    # cover - this is what actually fixes cross-faculty specialisation
    # units (Innovation and Entrepreneurship's SIEN/MKTG/DECO units, etc.)
    # that were previously stuck at prerequisite_codes=None despite the
    # real data existing in the relevant table_s_<faculty>_units.json all
    # along.
    table_s_applied = []
    for code, unit in merged.items():
        if unit["prerequisite_codes"] is not None:
            continue  # already covered by the stream table - don't override
        ts = table_s_units.get(code)
        if ts is None:
            continue
        unit["prerequisite_codes"] = ts.get("prerequisite_codes", [])
        unit["prerequisite_groups"] = ts.get("prerequisite_groups", [])
        unit["corequisite_codes"] = ts.get("corequisite_codes", [])
        unit["corequisite_groups"] = ts.get("corequisite_groups", [])
        unit["prohibition_codes"] = ts.get("prohibition_codes", [])
        unit["assumed_knowledge_codes"] = ts.get("assumed_knowledge_codes", [])
        table_s_applied.append(code)

    # Direct, hand-verified fix for the two units confirmed to be
    # mis-parsed (see PREREQUISITE_GROUP_OVERRIDES above) - applied after
    # the stream-table/Table S merges so it's the final word for these
    # two, regardless of what either produced.
    for code, override in PREREQUISITE_GROUP_OVERRIDES.items():
        if code in merged:
            merged[code]["prerequisite_codes"] = list(override["prerequisite_codes"])
            merged[code]["prerequisite_groups"] = [list(g) for g in override["prerequisite_groups"]]

    # Apply manual overrides for units CUSP never lists as discrete rows
    # (see module docstring) - added fresh if missing entirely, or
    # patched in place if a partial/wrong record already exists for some
    # other reason.
    overrides_added = []
    for code, override in MANUAL_UNIT_OVERRIDES.items():
        if code not in merged:
            overrides_added.append(code)
        merged[code] = dict(override)

    # Layer in structured credit-point-threshold requirements for units
    # that have one but weren't already fully covered by a manual
    # override above (currently just MATH2069).
    patched_credit_reqs = []
    for code, req in CREDIT_REQUIREMENT_PATCHES.items():
        if code in merged:
            merged[code]["credit_point_requirement"] = req
            patched_credit_reqs.append(code)

    # Apply the PEP progression chain: each unit after the first gets its
    # predecessor as a prerequisite (ordinary "must be earlier" semantics
    # for cascade/dependents purposes) AND immediate_successor_of (the
    # stricter "exactly the next semester" rule the planner enforces on
    # top).
    pep_chained = []
    for predecessor, code in zip(PEP_CHAIN, PEP_CHAIN[1:]):
        if code not in merged:
            continue
        existing = merged[code].get("prerequisite_codes") or []
        if predecessor not in existing:
            existing = existing + [predecessor]
        merged[code]["prerequisite_codes"] = existing
        merged[code]["immediate_successor_of"] = predecessor
        pep_chained.append(code)

    no_prereq_data = sorted(code for code, u in merged.items() if u["prerequisite_codes"] is None)

    print(f"Merged {len(merged)} unique units.")
    print(f"  {matched} matched against {STREAM_TABLE_FILE}'s prerequisite data.")
    print(f"  {len(no_prereq_data)} have no prerequisite data (outside the Software Stream "
          f"table - Intro Computing, Foundation Maths, PEP, Projects, Free Electives, etc.)")
    if no_prereq_data:
        preview = ", ".join(no_prereq_data[:12])
        more = f", ... (+{len(no_prereq_data) - 12} more)" if len(no_prereq_data) > 12 else ""
        print(f"    e.g. {preview}{more}")
    if unmatched:
        print(f"  [WARN] {len(unmatched)} stream-table units not found in any CUSP block - "
              f"check for code mismatches: {unmatched}")
    if table_s_applied:
        print(f"  Filled in {len(table_s_applied)} unit(s) from Table S data the stream table "
              f"doesn't cover: {table_s_applied}")
    if unit_sessions:
        print(f"  Loaded live session data for {len(unit_sessions)} units from {UNIT_SESSIONS_FILE}.")
        if session_corrections:
            print(f"  [CORRECTED] {len(session_corrections)} unit(s) where CUSP's session data was "
                  f"wrong - live page wins:")
            for code, old, new in session_corrections:
                print(f"    {code}: CUSP said {old}, live page says {new}")
        if discontinued_units:
            print(f"  [DISCONTINUED] {len(discontinued_units)} unit(s) confirmed no longer offered "
                  f"(flagged, not removed - see each unit's 'discontinued' field): {discontinued_units}")
    else:
        print(f"  [WARN] {UNIT_SESSIONS_FILE} not found - run fetch_unit_sessions.py to get "
              f"real per-unit session data instead of relying on CUSP's (confirmed stale for "
              f"at least one unit, and doesn't reliably flag discontinued units at all).")
    if overrides_added:
        print(f"  Added {len(overrides_added)} manually-verified unit(s) CUSP never lists as "
              f"discrete rows: {overrides_added}")
    if patched_credit_reqs:
        print(f"  Attached structured credit-point-threshold requirements to: {patched_credit_reqs}")
    if pep_chained:
        print(f"  Applied PEP immediate-succession chain to: {pep_chained}")
    specialisations = cusp_data.get("specialisations", [])
    if specialisations:
        stream_count = sum(1 for s in specialisations if s["type"] == "stream")
        breadth_count = sum(1 for s in specialisations if s["type"] == "breadth")
        print(f"  Carried through {len(specialisations)} specialisations "
              f"({stream_count} stream, {breadth_count} breadth).")
    else:
        print(f"  [WARN] No specialisations found in {CUSP_STRUCTURE_FILE} - "
              f"re-run scrape_cusp_degree.py if you expect this degree to have some.")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({
            "stream_info": stream_info,
            "semester_plan": cusp_data["semester_plan"],
            "specialisations": cusp_data.get("specialisations", []),
            "degree_blocks_meta": {
                name: {"min_cp": block["min_cp"], "max_cp": block["max_cp"]}
                for name, block in cusp_data["unit_blocks"].items()
            },
            "units": merged,
        }, f, indent=2)

    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. Units with prerequisite_codes=None still need their prereqs filled in
#    somehow before the DAG planner can treat the degree as fully
#    constrained - either by extending scale_live_fetch.py's requirements-
#    cell parser to also run against non-Software-Stream handbook pages
#    (Foundation Maths, PEP, etc. mostly have trivial/no prereqs, so this
#    may be low-value), or by live-fetching just those ~15-20 units'
#    individual handbook pages on demand.
# 2. degree_blocks lets the planner tell "core requirement" apart from
#    "elective option" - e.g. a unit in "Software Stream Core" must be
#    taken, one in "Software Stream 3000+ Level Elective Units" is a pool
#    to choose from up to the block's max_cp.
# 3. semester_plan (carried through unchanged from cusp_degree_structure.json)
#    is the official default sequence - useful as the planner's starting
#    point/sanity check before it generates or adapts a custom plan.
# 4. Generic credit-point-threshold prerequisites (ELEC4712's "36cp of
#    3000+ BE units", MATH2069's "6cp of MATH1XXX except (...)") ARE now
#    validated by the planner via credit_point_requirement, not just
#    documented - see curriculum_planner.py's _credit_requirement_satisfied().
#    Any OTHER unit with a similar wildcard requirement not listed in
#    CREDIT_REQUIREMENT_PATCHES above still won't be validated - this
#    mechanism is applied per-unit as they're identified, not derived
#    automatically from requirements_raw text.
# 5. specialisations and degree_blocks_meta are only present if
#    cusp_degree_structure.json was produced by the updated
#    scrape_cusp_degree.py (with parse_specialisations) - re-run that
#    scraper first if merged_degree_data.json ends up with an empty
#    specialisations list unexpectedly. specialisations identifies which
#    of the degree's specialisations are "stream" type vs "breadth" type
#    (only stated in the Pathways section headings, not in the block
#    names themselves) and which Core/Elective unit blocks each maps to;
#    degree_blocks_meta carries every block's min_cp/max_cp through,
#    which was previously lost during the merge (units only recorded
#    which blocks they belonged to, not each block's own CP requirement).