"""
Curriculum planner / timetable generator, built on merged_degree_data.json.

Core pieces:
  1. parse_default_schedule() - turns CUSP's official Year X - Semester Y
     structure (semester_plan) into a clean {semester_index: [unit_code,
     ...]} schedule. This is USYD's own default sequence, used as the
     starting point.
  2. validate_schedule() - checks every scheduled unit's prerequisites and
     corequisites are actually satisfied by where it's placed, using the
     prerequisite/corequisite data merged in from the stream table and
     Table S scrapes, INCLUDING generic credit-point-threshold
     requirements like ELEC4712's "36cp of 3000+ BE units" (see
     _credit_requirement_satisfied). This is the grounding check: the
     planner reasons over real requirement data pulled from the
     university's own pages, not an LLM guessing at a valid sequence.
  3. replan_after_failure() - the adaptive-replanning piece (Objective 2:
     "adapt the plan when circumstances change, such as after a failed
     unit"). Given a schedule and a unit the student failed, removes it,
     finds every unit that (directly or transitively) depended on it as a
     prerequisite, and pushes each forward only as far as needed.
  4. compact_schedule() - load-balances the result, prioritising
     finishing the degree in the standard 4 years (8 semesters) via
     overloading where legal, before falling back to a general
     top-up-to-24cp pass. See its docstring for the two-phase approach.

IMPORTANT - OR semantics for prerequisites/corequisites: the scraper
extracts every unit code mentioned in a "P ..." or "C ..." clause into one
flat list (e.g. INFO1113's prerequisite_codes = [INFO1105, INFO1103,
INFO1905]), but the underlying handbook text is "P INFO1105 or INFO1103 or
INFO1905" - alternative ways to satisfy ONE requirement, not three
independently mandatory units. Every prerequisite/corequisite check here
treats a unit's list of codes as "satisfy at least one of these".

IMPORTANT - credit-point-threshold requirements: some prerequisites can't
be expressed as a list of specific codes at all - e.g. ELEC4712 needs "36
credit points of 3000-or-higher level BE units of study", any of them,
not a fixed list. merged_degree_data.json represents these via an
optional credit_point_requirement field (min_credit_points, min_level,
max_level, prefix, excluded_patterns), and _credit_requirement_satisfied()
below actually validates it - summing the credit points of everything
scheduled earlier that matches, including elective slots at the right
level (a "3000+ Level Elective" placeholder genuinely represents 6cp at
that level even though the specific unit isn't decided).

Elective slots (Software Stream 1000/2000-level electives, 3000+-level
electives, Free Electives) are NOT auto-filled with a specific unit here -
which elective a student picks is their choice. Those slots stay as
placeholders (e.g. "ELECTIVE:Software Stream 3000+ Level Elective Units"),
sized by their required credit points.

DEPENDENCY: needs merged_degree_data.json from merge_degree_data.py.
Table S data is NOT required for this to work - electives stay as
placeholders regardless.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import json
import re
from collections import defaultdict, deque

MERGED_DATA_FILE = P("merged_degree_data.json")

# Target semester load: at least MIN, at most MAX, unless there simply
# aren't enough eligible units left to reach MIN (e.g. near the end of
# the degree). USYD full-time engineering load is normally 4x6cp units
# (24cp) - MAX allows a little headroom (one overload unit, a 5-unit
# semester) specifically so compaction can prioritise finishing in the
# standard 4 years over spilling into a 5th.
MIN_CP_PER_SEMESTER = 24
MAX_CP_PER_SEMESTER = 30

# Positions per year, in calendar order: Semester 1, a "winter gap"
# (between Sem 1 and Sem 2 of the SAME year - hosts USYD's June-July and
# July-August intensive sessions), Semester 2, then a "summer gap"
# (between this year's Sem 2 and next year's Sem 1 - hosts USYD's
# November-December, January, and January-February intensive sessions).
# Four positions per year instead of two, so intensive sessions get
# real, ordered positions in the SAME linear semester_index scheme every
# other function already relies on ("target < X" prerequisite checks
# etc. keep working unchanged), rather than a parallel structure bolted
# on the side. Confirmed against
# https://www.sydney.edu.au/students/intensive-sessions.html (teaching
# period 1 = Jan-Jun, teaching period 2 = Jul-Dec; each intensive
# session's own SITS code prefix, S1/S2, indicates which teaching period
# it belongs to administratively) - see _session_gap_types.
POSITIONS_PER_YEAR = 4
POS_SEM1 = 0
POS_WINTER_GAP = 1
POS_SEM2 = 2
POS_SUMMER_GAP = 3
_POSITION_KIND_BY_OFFSET = {
    POS_SEM1: "sem1",
    POS_WINTER_GAP: "winter_gap",
    POS_SEM2: "sem2",
    POS_SUMMER_GAP: "summer_gap",
}

# 4 years x 4 positions/year. compact_schedule() treats staying within
# this many positions as the priority, overloading earlier REAL
# semesters up to MAX_CP_PER_SEMESTER, and only reaching for a gap
# (intensive) slot as a last resort, before accepting a position beyond
# this index (a 5th year).
TARGET_SEMESTERS = 4 * POSITIONS_PER_YEAR

# Every elective slot in this degree (Software Stream 1000/2000-level,
# 3000+-level, and Free Electives) is drawn from 6cp units - there are no
# other sizes on offer, so an unresolved ELECTIVE: placeholder can be
# counted as 6cp with confidence, same as a real unit of that size.
ELECTIVE_SLOT_CP = 6

# Hard safety bound on how many REAL semesters forward the replanning
# search will look before giving up on a unit. Should never normally be
# hit - exists as a backstop so any unforeseen unsatisfiable case fails
# loudly with a clear message instead of hanging.
MAX_SEARCH_HORIZON_SEMESTERS = 20

UNIT_CODE_PATTERN = re.compile(r"\b([A-Z]{2,5}\d{3,4})\b")
UNIT_LEVEL_PATTERN = re.compile(r"^[A-Z]{2,5}(\d)\d{2,3}$")

# Intensive-session recognition, beyond the plain "Semester 1"/"Semester
# 2" substrings unit_offered_in() already handles for real semesters.
# USYD's five current intensive sessions, and which gap type each falls
# in (confirmed against sydney.edu.au/students/intensive-sessions.html):
#   - Intensive January (S1CIJA)           -> summer gap (before Sem 1)
#   - Intensive January-February (S1CIFE)  -> summer gap (before Sem 1)
#   - Intensive June-July (S2CIJL)         -> winter gap (between Sem 1/2)
#   - Intensive July-August (S2CIAU)       -> winter gap (between Sem 1/2)
#   - Intensive November-December (S2CIDE) -> summer gap (after Sem 2)
# CUSP's own handbook pages use descriptive text rather than these SITS
# codes - the one confirmed real example seen in scraped data is
# "Int January" (on CIVL5330). The patterns below are inferred from that
# single example plus the official session names above - NOT yet
# verified against a real scraped "Int June-July" or "Int
# November-December" string. If a scrape ever shows a different actual
# label, update these patterns.
INTENSIVE_SESSION_PATTERNS = {
    "summer": [
        re.compile(r"int.*jan", re.IGNORECASE),
        re.compile(r"int.*nov", re.IGNORECASE),
        re.compile(r"int.*dec", re.IGNORECASE),
    ],
    "winter": [
        re.compile(r"int.*jun", re.IGNORECASE),
        re.compile(r"int.*jul", re.IGNORECASE),
        re.compile(r"int.*aug", re.IGNORECASE),
    ],
}


def _position_kind(sem_index):
    """
    Returns 'sem1', 'winter_gap', 'sem2', or 'summer_gap' for a given
    semester_index, under the 4-positions-per-year scheme (see
    POSITIONS_PER_YEAR).
    """
    return _POSITION_KIND_BY_OFFSET[sem_index % POSITIONS_PER_YEAR]


def _position_label(sem_index):
    """Human-readable label, e.g. 'Year 2 - Semester 1' or 'Year 2 - Winter Intensive (Jun-Aug)'."""
    year = sem_index // POSITIONS_PER_YEAR + 1
    kind = _position_kind(sem_index)
    if kind == "sem1":
        return f"Year {year} - Semester 1"
    if kind == "sem2":
        return f"Year {year} - Semester 2"
    if kind == "winter_gap":
        return f"Year {year} - Winter Intensive (Jun-Aug)"
    return f"Year {year} - Summer Intensive (Nov-Feb)"


def _next_real_index(sem_index):
    """Advances to the next REAL semester position (sem1/sem2), skipping any gap slot in between."""
    sem_index += 1
    while _position_kind(sem_index) not in ("sem1", "sem2"):
        sem_index += 1
    return sem_index


def _previous_real_index(sem_index):
    """Steps back to the previous REAL semester position, skipping any gap slot in between."""
    sem_index -= 1
    while sem_index >= 0 and _position_kind(sem_index) not in ("sem1", "sem2"):
        sem_index -= 1
    return sem_index


def _ordered_candidates(limit):
    """
    Every position index in [0, limit), ordered so REAL semesters
    (sem1/sem2) are all tried before any gap (intensive) slot - "prefer
    a normal semester enrolment unless it delays graduation." Both
    groups are internally in ascending index order.
    """
    real = [i for i in range(limit) if _position_kind(i) in ("sem1", "sem2")]
    gaps = [i for i in range(limit) if _position_kind(i) in ("winter_gap", "summer_gap")]
    return real + gaps


def _session_gap_types(sessions):
    """
    Given a unit's sessions_offered list, returns the set of gap types
    ("summer", "winter") it's offered in via an intensive session - see
    INTENSIVE_SESSION_PATTERNS.
    """
    gap_types = set()
    for s in sessions or []:
        for gap_type, patterns in INTENSIVE_SESSION_PATTERNS.items():
            if any(p.search(s) for p in patterns):
                gap_types.add(gap_type)
    return gap_types


def load_merged_data():
    with open(MERGED_DATA_FILE) as f:
        return json.load(f)


PEP_BLOCK_NAME = "Professional Engagement Program (PEP) Units"


def _is_pep_unit(code, merged_data):
    """
    True if `code` belongs to the degree's PEP (Professional Engagement
    Program) block. Detected generically via degree_blocks membership
    (the same "Professional Engagement Program (PEP) Units" block name
    CUSP uses across engineering degrees), not a hardcoded code prefix.
    """
    blocks = merged_data["units"].get(code, {}).get("degree_blocks") or []
    return PEP_BLOCK_NAME in blocks


def parse_default_schedule(merged_data):
    """
    Turn semester_plan into {semester_index: [unit_code_or_placeholder, ...]}.
    Real teaching semesters get indices under the 4-positions-per-year
    scheme (see POSITIONS_PER_YEAR) - Year 1 Sem 1 = 0, Year 1 Sem 2 = 2,
    Year 2 Sem 1 = 4, Year 2 Sem 2 = 6, etc. - leaving the odd-offset
    "gap" positions (1 and 3 within each year) unoccupied here, for
    intensive-session placements to use later. Skips 'Year 0 - Planning
    your degree'. An entry naming a specific unit code takes the first
    as the default choice; an entry naming only elective Unit Blocks
    becomes an "ELECTIVE:<block name>" placeholder.

    PEP (Professional Engagement Program) units are deliberately EXCLUDED
    here - they're not part of the constraint-solved schedule at all.
    USYD's standard progression is one-per-semester, but that's not a
    hard rule (a student can complete multiple PEP units in the same
    semester); the only real requirement is finishing the whole program
    by graduation. Treating it as a strict scheduling constraint caused
    real problems (a failed PEP unit's retake becomes mathematically
    impossible under a strict "immediately following" rule, since the
    one valid slot has already passed - confirmed by an automated stress
    test). Instead, generate_pep_reference_table() produces a simple,
    always-the-same informational sample pathway shown separately from
    the optimized schedule - see main()'s printed notice.
    """
    schedule = {}
    real_semester_count = 0

    for period in merged_data["semester_plan"]:
        if period["period"].startswith("Year 0"):
            continue

        year_num = real_semester_count // 2
        pos = POS_SEM1 if real_semester_count % 2 == 0 else POS_SEM2
        sem_index = year_num * POSITIONS_PER_YEAR + pos

        codes_this_semester = []
        for entry in period["entries"]:
            raw = entry["raw_options"]
            codes = UNIT_CODE_PATTERN.findall(raw)
            if codes:
                if _is_pep_unit(codes[0], merged_data):
                    continue
                codes_this_semester.append(codes[0])
            else:
                block_match = re.search(r"Unit Blocks?:\s*(.+)$", raw)
                block_names = block_match.group(1) if block_match else raw

                codes_this_semester.append(f"ELECTIVE:{block_names}")

        schedule[sem_index] = codes_this_semester
        real_semester_count += 1

    return schedule


def generate_pep_reference_table(merged_data):
    """
    A simple, always-the-same informational sample: assigns each PEP unit
    (in its standard order - ENGP1001, ENGP1002, ...) to one real semester
    each, in sequence (Year 1 Sem 1, Year 1 Sem 2, Year 2 Sem 1, ...).
    This is NOT a constraint-solved placement and doesn't respond to
    failures, specialisation choice, or anything else - it's a reference
    pathway to show alongside the real schedule, since PEP units are
    excluded from that schedule entirely (see parse_default_schedule).

    Returns a list of (semester_index, code, name) tuples in order.
    """
    pep_codes = [
        code for code in merged_data["units"]
        if _is_pep_unit(code, merged_data)
    ]
    # Sort by the numeric part of the code (ENGP1001, ENGP1002, ...) for
    # a stable, sensible default order rather than relying on dict
    # iteration order.
    pep_codes.sort(key=lambda c: re.search(r"\d+", c).group())

    table = []
    real_sem_indices = [
        i * POSITIONS_PER_YEAR + pos
        for i in range(TARGET_SEMESTERS // POSITIONS_PER_YEAR)
        for pos in (POS_SEM1, POS_SEM2)
    ]
    for code, sem_index in zip(pep_codes, real_sem_indices):
        name = merged_data["units"].get(code, {}).get("name", "Unknown unit")
        table.append((sem_index, code, name))
    return table


def print_pep_notice(merged_data):
    """
    Prints the PEP program notice: an explanation that PEP units aren't
    part of the optimized schedule above, a sample one-per-semester
    pathway, and a pointer to the faculty/university for actual planning
    guidance - per the design decision to keep PEP out of the constraint
    solver entirely (see parse_default_schedule's docstring).
    """
    print("=" * 70)
    print("[NOTICE] Professional Engagement Program (PEP)")
    print("=" * 70)
    print(
        "PEP units are not included in the optimised schedule above. "
        "USYD does not require exactly one PEP unit per semester - you "
        "can complete more than one in the same semester - but the full "
        "PEP program must be completed by the time you graduate.\n"
    )
    print("Sample one-per-semester pathway (not personalised, for reference only):")
    for sem_index, code, name in generate_pep_reference_table(merged_data):
        print(f"  {_position_label(sem_index)}: {code} - {name}")
    print(
        "\nFor guidance on planning your own PEP progression, contact "
        "your faculty or course coordinator."
    )
    print()


def build_prereq_graph(merged_data):
    """
    Returns (prereqs, coreqs, dependents):
      prereqs[code]    -> set of codes, ANY ONE of which satisfies the
                          prerequisite (OR semantics - see module docstring)
      coreqs[code]     -> set of codes, ANY ONE of which satisfies the
                          corequisite, taken same semester or earlier
      dependents[code] -> set of codes that list `code` as one of their
                          prerequisite OR corequisite alternatives (the
                          reverse edge, used for replanning cascades).
                          Includes corequisite relationships too - if a
                          unit's corequisite gets displaced by a failure,
                          the unit depending on it needs to be rechecked
                          just as much as one depending on it as an
                          ordinary prerequisite would.
    """
    prereqs = defaultdict(set)
    coreqs = defaultdict(set)
    dependents = defaultdict(set)

    for code, unit in merged_data["units"].items():
        for p in (unit.get("prerequisite_codes") or []):
            prereqs[code].add(p)
            dependents[p].add(code)
        for c in (unit.get("corequisite_codes") or []):
            coreqs[code].add(c)
            dependents[c].add(code)

    return prereqs, coreqs, dependents


def _base_code(code):
    """Strip the FAILED: marker prefix, if present, to get the real unit code."""
    return code[len("FAILED:"):] if code.startswith("FAILED:") else code


def _semester_of_map(schedule):
    """
    Maps a unit code to the semester it's scheduled in - only entries
    that represent an actually-completed placement. ELECTIVE: and
    FAILED: entries are excluded (see their module-docstring notes).
    """
    semester_of = {}
    for sem_index, codes in schedule.items():
        for code in codes:
            if code.startswith("ELECTIVE:") or code.startswith("FAILED:"):
                continue
            semester_of[code] = sem_index
    return semester_of


def _unit_level(code):
    """
    Extract a unit code's numeric 'level' for credit-point-threshold
    checks, e.g. COMP3027 -> 3000, MATH1061 -> 1000. Matches how USYD
    itself describes levels ("1000-level", "3000-level", ...). Returns
    None if the code doesn't match the expected shape.
    """
    match = UNIT_LEVEL_PATTERN.match(code)
    if not match:
        return None
    return int(match.group(1)) * 1000


def _credit_requirement_satisfied(code, target_sem, schedule, merged_data):
    """
    Validates a generic "N credit points of [prefix][level range] units
    [except (...)]" prerequisite (credit_point_requirement in
    merged_degree_data.json - see ELEC4712/MATH2069) by summing the
    credit points of everything already scheduled strictly before
    target_sem that matches the requirement's prefix/level range and
    isn't excluded. A FAILED: attempt doesn't count (it didn't actually
    earn the credit). An ELECTIVE: placeholder counts if its block name
    indicates it will be filled at/above the required level (e.g. an
    "ELECTIVE:Software Stream 3000+ Level Elective Units" slot
    genuinely represents 6cp of 3000+-level credit, even though the
    specific unit isn't decided) - only applied for a plain "at least
    this level" requirement (max_level is None), since a bounded range
    (like MATH2069's exactly-1000-level requirement) can't be confirmed
    from a generic elective placeholder's name.

    Returns True if the unit has no credit_point_requirement (nothing to
    check here - either no prerequisite, or a code-list one handled by
    _prereq_satisfied instead).
    """
    req = merged_data["units"].get(code, {}).get("credit_point_requirement")
    if not req:
        return True

    min_cp = req["min_credit_points"]
    min_level = req.get("min_level")
    max_level = req.get("max_level")
    prefix = req.get("prefix")
    excluded_patterns = [re.compile(p) for p in req.get("excluded_patterns", [])]

    total = 0
    for sem_index, codes in schedule.items():
        if sem_index >= target_sem:
            continue
        for c in codes:
            if c.startswith("FAILED:"):
                continue
            if c.startswith("ELECTIVE:"):
                if max_level is None and min_level is not None:
                    block_name = c[len("ELECTIVE:"):]
                    if f"{min_level // 1000}000+" in block_name:
                        total += ELECTIVE_SLOT_CP
                continue

            if prefix and not c.startswith(prefix):
                continue
            level = _unit_level(c)
            if level is None:
                continue
            if min_level is not None and level < min_level:
                continue
            if max_level is not None and level > max_level:
                continue
            if any(p.fullmatch(c) for p in excluded_patterns):
                continue

            cp = merged_data["units"].get(c, {}).get("credit_points") or 0
            total += cp

    return total >= min_cp


def _prereq_satisfied(code, target_sem, schedule, semester_of, prereqs, merged_data):
    """
    A unit's prerequisite is satisfied by, in priority order:
      1. prerequisite_groups, if present - AND-of-OR structure (e.g.
         ELEC3609 needs one code from EACH of three separate groups, not
         any single code from a flat combined list). Every group must
         have at least one alternative scheduled strictly before
         target_sem.
      2. prerequisite_codes (flat OR list) - at least one alternative
         scheduled strictly before target_sem. Used when there's no
         group structure (the common case: a single OR-group, or no
         prerequisite at all).
      3. credit_point_requirement - a generic "Ncp of [level range]
         units" threshold, validated by summing matching earlier credit.
    A unit with none of the above is trivially satisfied. (No unit in
    this dataset currently mixes more than one of these types.)
    """
    groups = merged_data["units"].get(code, {}).get("prerequisite_groups")
    if groups:
        return all(
            any(p in semester_of and semester_of[p] < target_sem for p in group)
            for group in groups
        )

    required_codes = prereqs.get(code, set())
    if required_codes:
        return any(p in semester_of and semester_of[p] < target_sem for p in required_codes)

    credit_req = merged_data["units"].get(code, {}).get("credit_point_requirement")
    if credit_req:
        return _credit_requirement_satisfied(code, target_sem, schedule, merged_data)

    return True


def _immediate_successor_satisfied(code, target_sem, semester_of, merged_data):
    """
    Some units (PEP progression: ENGP1001 -> ENGP1002 -> ... -> ENGP3002)
    must be taken in the semester IMMEDIATELY following their predecessor
    - not just any earlier semester like an ordinary prerequisite.
    Confirmed via individual PEP handbook pages, e.g. ENGP1003: "Students
    must enrol in ENGP1003 in the semester immediately following
    completion of ENGP1002." A unit with no immediate_successor_of field
    is trivially satisfied (this check doesn't apply to it).

    Uses _previous_real_index(target_sem), not target_sem - 1: under the
    4-positions-per-year scheme (see POSITIONS_PER_YEAR), the position
    immediately before a real semester is a gap slot, not the previous
    real semester - PEP units are never offered in a gap slot
    (unit_offered_in() only allows a gap slot with confirmed intensive-
    session data, which PEP units don't have), so "immediately following"
    always means the previous REAL semester specifically.
    """
    predecessor = merged_data["units"].get(code, {}).get("immediate_successor_of")
    if not predecessor:
        return True
    if predecessor not in semester_of:
        return False
    return semester_of[predecessor] == _previous_real_index(target_sem)


def _requirements_satisfied(code, target_sem, schedule, semester_of, prereqs, merged_data):
    """
    Combined check used wherever a single pass/fail is needed (replanning
    search, compaction eligibility) - both the prerequisite (code-list,
    grouped, or credit-threshold) AND the immediate-succession rule (if
    any) must hold. validate_schedule() checks these separately instead,
    so it can report which one actually failed.
    """
    return (
        _prereq_satisfied(code, target_sem, schedule, semester_of, prereqs, merged_data)
        and _immediate_successor_satisfied(code, target_sem, semester_of, merged_data)
    )


def _coreq_satisfied(code, target_sem, semester_of, coreqs):
    """
    OR semantics, satisfied by the same semester or earlier (you can take
    a corequisite unit concurrently, or have already completed it).
    """
    required = coreqs.get(code, set())
    if not required:
        return True
    return any(c in semester_of and semester_of[c] <= target_sem for c in required)


def _prohibition_satisfied(code, semester_of, merged_data):
    """
    True if placing `code` wouldn't violate a mutual-exclusion
    (prohibition) rule against any unit already scheduled anywhere in
    the plan - e.g. the regular and advanced version of the same
    subject, which typically can't both count. Not time-based like a
    prerequisite: a prohibition applies regardless of when either unit
    is taken, so this checks against every currently-scheduled unit, not
    just earlier ones.

    Checked in BOTH directions - code's own prohibition_codes against
    everything scheduled, AND every scheduled unit's prohibition_codes
    against code - since the raw handbook data isn't necessarily
    recorded symmetrically (e.g. an advanced unit's page may say
    "N <regular unit>" without the regular unit's own page listing the
    advanced one back).
    """
    code_prohibits = set(merged_data["units"].get(code, {}).get("prohibition_codes") or [])
    for other in semester_of:
        if other == code:
            continue
        if other in code_prohibits:
            return False
        other_prohibits = set(merged_data["units"].get(other, {}).get("prohibition_codes") or [])
        if code in other_prohibits:
            return False
    return True


def validate_schedule(schedule, prereqs, coreqs, merged_data):
    """
    Checks every scheduled (non-elective-placeholder) unit's prerequisite
    (grouped AND-of-OR, flat OR, or credit-point-threshold), immediate-
    succession rule (if any), corequisites, and prohibitions (mutual
    exclusions - e.g. can't count both the regular and advanced version
    of the same subject) are satisfied by where it's placed. Returns a
    list of violation strings - empty means the schedule is internally
    consistent with the requirement data.
    """
    semester_of = _semester_of_map(schedule)
    violations = []

    for code, sem_index in semester_of.items():
        if not _prereq_satisfied(code, sem_index, schedule, semester_of, prereqs, merged_data):
            unit = merged_data["units"].get(code, {})
            groups = unit.get("prerequisite_groups")
            credit_req = unit.get("credit_point_requirement")
            if groups:
                unmet = [
                    g for g in groups
                    if not any(p in semester_of and semester_of[p] < sem_index for p in g)
                ]
                unmet_desc = "; ".join(" or ".join(g) for g in unmet)
                violations.append(
                    f"{code} (sem {sem_index}) is missing at least one unit from each of these "
                    f"prerequisite group(s), scheduled earlier: {unmet_desc}"
                )
            elif credit_req:
                violations.append(
                    f"{code} (sem {sem_index}) needs {credit_req['min_credit_points']}cp of "
                    f"matching units scheduled earlier - not enough are."
                )
            else:
                alts = ", ".join(sorted(prereqs.get(code, set())))
                violations.append(
                    f"{code} (sem {sem_index}) needs at least one of its prerequisites "
                    f"({alts}) scheduled in an earlier semester - none currently are."
                )
        if not _immediate_successor_satisfied(code, sem_index, semester_of, merged_data):
            predecessor = merged_data["units"].get(code, {}).get("immediate_successor_of")
            violations.append(
                f"{code} (sem {sem_index}) must be taken in the semester immediately "
                f"following {predecessor} - it currently isn't."
            )
        if not _coreq_satisfied(code, sem_index, semester_of, coreqs):
            alts = ", ".join(sorted(coreqs.get(code, set())))
            violations.append(
                f"{code} (sem {sem_index}) needs at least one of its corequisites "
                f"({alts}) scheduled in the same semester or earlier - none currently are."
            )
        if not _prohibition_satisfied(code, semester_of, merged_data):
            code_prohibits = set(merged_data["units"].get(code, {}).get("prohibition_codes") or [])
            conflicts = sorted(
                other for other in semester_of
                if other != code and (
                    other in code_prohibits
                    or code in set(merged_data["units"].get(other, {}).get("prohibition_codes") or [])
                )
            )
            violations.append(
                f"{code} (sem {sem_index}) is mutually exclusive with already-scheduled "
                f"unit(s): {', '.join(conflicts)} - both can't count toward the degree."
            )

    return violations


def semester_credit_points(schedule, sem_index, merged_data):
    """
    Total committed credit points for a semester. ELECTIVE: placeholders
    count as ELECTIVE_SLOT_CP each - they represent a real 6cp commitment
    even though which specific unit fills the slot isn't decided here.
    """
    total = 0
    for code in schedule.get(sem_index, []):
        if code.startswith("ELECTIVE:"):
            total += ELECTIVE_SLOT_CP
            continue
        cp = merged_data["units"].get(_base_code(code), {}).get("credit_points")
        total += cp or 0
    return total


def unit_offered_in(code, sem_index, merged_data):
    """
    True if the unit is offered in the session sem_index represents - a
    real Semester 1/2, or (see POSITIONS_PER_YEAR) a winter/summer
    intensive gap slot.

    A unit flagged discontinued (see scrape_unit_sessions.py - confirmed
    directly against its own live page, not assumed) is never offered
    anywhere, regardless of whatever sessions_offered still says -
    checked here rather than at each individual candidate-building
    function, since every placement path already calls this before
    accepting a candidate for a slot.

    For a REAL semester position: missing sessions_offered data or a
    literal "Do not offer" both return True rather than blocking
    scheduling on data that can never resolve to a match.

    For a GAP (intensive) position: the default flips - missing data
    returns False. Intensive sessions are relatively rare and only a
    minority of units run in them, so defaulting to "yes, offered" the
    way real semesters do would risk scheduling something into an
    intensive it was never actually confirmed to run in. A gap slot is
    only usable when sessions_offered explicitly confirms a matching
    intensive session (see _session_gap_types).
    """
    unit = merged_data["units"].get(code, {})
    if unit.get("discontinued"):
        return False

    sessions = unit.get("sessions_offered")
    kind = _position_kind(sem_index)

    if kind in ("winter_gap", "summer_gap"):
        if not sessions:
            return False
        wanted = "winter" if kind == "winter_gap" else "summer"
        return wanted in _session_gap_types(sessions)

    if not sessions:
        return True
    if all(s.strip().lower() == "do not offer" for s in sessions):
        return True

    wants_sem1 = kind == "sem1"
    has_sem1 = any("Semester 1" in s for s in sessions)
    has_sem2 = any("Semester 2" in s for s in sessions)
    return has_sem1 if wants_sem1 else has_sem2


def replan_after_failure(schedule, failed_code, merged_data, prereqs, dependents, coreqs):
    """
    The adaptive-replanning operation: the student failed `failed_code`.
    Marks its original slot as FAILED:<code> (still counted toward that
    semester's credit load, but not toward satisfying prerequisites -
    see _semester_of_map), finds every unit that (directly or
    transitively) depends on it as a prerequisite alternative, and pushes
    each forward until it's satisfied again, offered, and has room.
    failed_code itself is rescheduled starting the semester AFTER the one
    it was failed in. Returns a new schedule dict; does not mutate input.

    Raises RuntimeError if a unit can't be placed within
    MAX_SEARCH_HORIZON_SEMESTERS semesters - should not normally trigger,
    exists as a hard backstop against hanging on an unforeseen case.
    """
    new_schedule = {sem: list(codes) for sem, codes in schedule.items()}
    semester_of = _semester_of_map(new_schedule)

    if failed_code not in semester_of:
        raise ValueError(f"{failed_code} isn't in the current schedule - nothing to replan.")

    original_sem = semester_of[failed_code]
    idx = new_schedule[original_sem].index(failed_code)
    new_schedule[original_sem][idx] = f"FAILED:{failed_code}"
    del semester_of[failed_code]

    to_recheck = deque([failed_code])
    affected_order = []
    seen = {failed_code}
    while to_recheck:
        current = to_recheck.popleft()
        for dep in sorted(dependents.get(current, set())):
            if dep in semester_of and dep not in seen:
                seen.add(dep)
                affected_order.append(dep)
                to_recheck.append(dep)

    for code in [failed_code] + affected_order:
        earliest = _next_real_index(original_sem) if code == failed_code else semester_of[code]
        target = earliest
        steps = 0
        if target not in new_schedule:
            new_schedule[target] = []

        # A unit with a STRICT immediate-succession requirement (e.g. a
        # PEP unit) can never be validly RETAKEN under that same strict
        # rule: the one exact slot "immediately following its
        # predecessor" is the slot it just failed in, which has already
        # passed, and the search only ever looks forward. Relaxing this
        # to the unit's ordinary prerequisite check for its own retake
        # only - which, for a PEP unit, already requires the same
        # predecessor to be scheduled strictly earlier, since PEP units
        # carry that predecessor in prerequisite_codes too - is the most
        # that's actually achievable once the unit has failed. Every
        # cascaded DEPENDENT of this unit (processed later in this same
        # loop) still enforces the strict rule as normal, which correctly
        # cascades a PEP-style chain forward by whatever gap the retake
        # introduced, rather than leaving them impossibly pinned to a
        # slot that's now unreachable too.
        relax_succession = code == failed_code and merged_data["units"].get(code, {}).get("immediate_successor_of")

        while True:
            prereqs_ok = _prereq_satisfied(code, target, new_schedule, semester_of, prereqs, merged_data)
            succession_ok = True if relax_succession else _immediate_successor_satisfied(code, target, semester_of, merged_data)
            coreqs_ok = _coreq_satisfied(code, target, semester_of, coreqs)
            prohibition_ok = _prohibition_satisfied(code, semester_of, merged_data)
            offered = unit_offered_in(code, target, merged_data)
            cp = merged_data["units"].get(code, {}).get("credit_points") or 0
            # Caps at MIN_CP_PER_SEMESTER, not MAX - this search never
            # overloads a semester on its own initiative. All deliberate
            # overloading (one failure "buys" roughly one overloaded
            # semester of slack) happens in exactly one place:
            # compact_schedule()'s failure-budgeted pass, which runs
            # right after this and decides which semester actually
            # deserves the extra room. If this search overloaded freely
            # here too, it could burn through several semesters' worth
            # of headroom on whichever unit it happens to place first,
            # leaving nothing for compact_schedule to give the unit that
            # actually needs it most.
            has_room = semester_credit_points(new_schedule, target, merged_data) + cp <= MIN_CP_PER_SEMESTER

            if prereqs_ok and succession_ok and coreqs_ok and prohibition_ok and offered and has_room:
                break

            # Only ever steps to the next REAL semester (never a gap/
            # intensive slot) - this search always prefers a normal
            # semester; using an intensive as a last resort is decided
            # later, in compact_schedule()'s tiered search.
            target = _next_real_index(target)
            steps += 1
            if steps > MAX_SEARCH_HORIZON_SEMESTERS:
                raise RuntimeError(
                    f"Couldn't find a valid semester for {code} within "
                    f"{MAX_SEARCH_HORIZON_SEMESTERS} semesters of sem {earliest}. This "
                    f"points to a genuine data issue - check {code}'s prerequisite_codes, "
                    f"credit_point_requirement, and sessions_offered in merged_degree_data.json."
                )
            if target not in new_schedule:
                new_schedule[target] = []

        if code != failed_code:
            new_schedule[semester_of[code]].remove(code)
        new_schedule[target].append(code)
        semester_of[code] = target

    return new_schedule


def _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=MAX_CP_PER_SEMESTER):
    """
    Shared eligibility check used by compact_schedule(): would moving
    `code` into semester `target` (from wherever it currently is)
    respect its prerequisites (grouped/flat/credit-threshold), the
    immediate-succession rule (if any - e.g. PEP progression), its
    corequisites, actual offering, the given credit-point cap, and NOT
    collide with an existing entry for the same unit already in `target`
    (e.g. a FAILED:<code> marker from an earlier failed attempt - moving
    the successful retake into the same semester it was failed in makes
    no sense, and previously happened via this function having no such
    check)? Uses the CURRENT (possibly already-partially compacted) state
    of new_schedule for the check, same as the live replanning search does.

    `cap` defaults to MAX_CP_PER_SEMESTER but callers doing budgeted
    overload (see compact_schedule) pass a tighter one - MIN_CP_PER_SEMESTER
    for a semester that hasn't earned overload room yet.
    """
    semester_of = _semester_of_map(new_schedule)
    base = _base_code(code)
    already_present = any(
        _base_code(c) == base for c in new_schedule.get(target, [])
    )
    if already_present:
        return False
    cp = merged_data["units"].get(code, {}).get("credit_points") or 0
    if semester_credit_points(new_schedule, target, merged_data) + cp > cap:
        return False
    if not _prereq_satisfied(code, target, new_schedule, semester_of, prereqs, merged_data):
        return False
    if not _immediate_successor_satisfied(code, target, semester_of, merged_data):
        return False
    if not _coreq_satisfied(code, target, semester_of, coreqs):
        return False
    if not _prohibition_satisfied(code, semester_of, merged_data):
        return False
    if not unit_offered_in(code, target, merged_data):
        return False
    return True


def _prune_empty_semesters(schedule):
    """
    Drop any semester key whose list is empty. These appear when the
    replanning search speculatively creates a new semester slot while
    looking for room for a unit (target not in new_schedule ->
    new_schedule[target] = []), but compaction later drains everything
    back into earlier semesters - leaving a placeholder key with no
    actual content. An empty semester isn't part of the real plan and
    shouldn't be printed or flagged as under-loaded.
    """
    return {sem: codes for sem, codes in schedule.items() if codes}


def compact_schedule(schedule, merged_data, prereqs, coreqs):
    """
    Two-phase load-balancing pass, in priority order:

    Phase 1 - collapse toward TARGET_SEMESTERS (4 years), most-constrained
    units first, within a failure-sized overload budget: for any unit at
    or beyond index TARGET_SEMESTERS, find how many semesters within
    0..TARGET_SEMESTERS-1 it could legally move into right now. Units
    with FEWER options are placed first - a unit that can only ever go in
    one specific semester (e.g. a Semester-2-only unit whose prerequisite
    itself just got pushed late) needs to claim that room before a
    flexible unit fills it first and strands it.

    Semesters aren't freely overloaded to MAX_CP_PER_SEMESTER, though -
    each semester starts capped at MIN_CP_PER_SEMESTER (24cp, no
    overload), and only gets permission to grow to MAX_CP_PER_SEMESTER
    (30cp) once the running count of already-overloaded semesters is
    still below the number of FAILED: markers in the schedule. The idea:
    one failure creates roughly one semester's worth of disruption, so
    it "buys" roughly one overloaded semester of slack to absorb it back
    within 4 years - not unlimited overloading just because the ceiling
    technically allows it. A semester already over 24cp keeps its
    existing room regardless (no double-charging against the budget for
    a semester already in use). Every placement attempt tries a
    non-overloading slot first and only reaches for budgeted overload
    room if nothing else works, so budget is never spent by a unit that
    had a free alternative.

    If a unit STILL has nowhere to go, it may be because whichever
    prerequisite is satisfying it is sitting later than it needs to -
    e.g. ELEC3609 needs ISYS2110 done first and is Semester-2-only; if
    ISYS2110 is sitting in the last Semester-1 slot available, ELEC3609
    has nowhere left. In that case, this also tries moving that blocking
    prerequisite itself to an earlier semester (checking every earlier
    semester, not just one step back) - if that succeeds, the stranded
    unit gets another chance next round.

    All of this - draining stranded units, nudging blockers earlier, and
    topping up under-filled semesters - runs as ONE combined loop that
    keeps repeating until a full pass makes no further changes anywhere.
    This matters because these three things can unblock each other: e.g.
    the top-up step might reposition a prerequisite in a way that opens a
    slot for a unit the drain step gave up on earlier - running each
    piece only once, in a fixed order, would miss that (confirmed: this
    is exactly what happened before this fix, with ISYS2110's top-up
    move to Year 4 Sem 1 coming too late for ELEC3609 to benefit, since
    draining had already given up by then).

    This is a greedy heuristic re-evaluated every round, not a globally
    optimal rebalance - sufficient given how few specifically-named units
    exist per semester in this dataset (the rest are elective
    placeholders, which never move). A hard round cap guards against a
    genuine oscillation (unlikely, but not proven impossible) hanging
    the process instead of terminating.
    """
    new_schedule = {sem: list(codes) for sem, codes in schedule.items()}

    num_failures = sum(
        1 for codes in new_schedule.values() for c in codes if c.startswith("FAILED:")
    )

    def overloaded_semester_count():
        return sum(
            1 for sem in range(TARGET_SEMESTERS)
            if _position_kind(sem) in ("sem1", "sem2")
            and semester_credit_points(new_schedule, sem, merged_data) > MIN_CP_PER_SEMESTER
        )

    def cap_for(sem_index):
        # A semester already over the baseline keeps its room regardless
        # (already "spent" from the budget) - only a still-baseline
        # semester needs budget left to be allowed to grow.
        if semester_credit_points(new_schedule, sem_index, merged_data) > MIN_CP_PER_SEMESTER:
            return MAX_CP_PER_SEMESTER
        if overloaded_semester_count() < num_failures:
            return MAX_CP_PER_SEMESTER
        return MIN_CP_PER_SEMESTER

    def count_options(code, candidates):
        return sum(
            1 for target in candidates
            if _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=cap_for(target))
        )

    def try_place(code, current_sem, candidates):
        """Tier 1 (no overload) first, then Tier 2 (budgeted overload). Mutates new_schedule on success."""
        for target in candidates:
            if _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=MIN_CP_PER_SEMESTER):
                new_schedule[current_sem].remove(code)
                new_schedule[target].append(code)
                return True
        for target in candidates:
            if _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=cap_for(target)):
                new_schedule[current_sem].remove(code)
                new_schedule[target].append(code)
                return True
        return False

    def nudge_blocker_earlier(code):
        """
        `code` currently has no legal semester anywhere in the window.
        For each of its prerequisite groups, find the LATEST-scheduled
        unit currently satisfying that group (the thing actually setting
        code's earliest option), and try moving that unit to any earlier
        semester. Returns True if a nudge succeeded.
        """
        semester_of = _semester_of_map(new_schedule)
        groups = merged_data["units"].get(code, {}).get("prerequisite_groups")
        req_groups = groups if groups else (
            [sorted(prereqs.get(code, set()))] if prereqs.get(code) else []
        )
        blockers = []
        for group in req_groups:
            scheduled = [(semester_of[p], p) for p in group if p in semester_of]
            if scheduled:
                blockers.append(max(scheduled))
        for sem_p, p in sorted(blockers, reverse=True):
            if try_place(p, sem_p, _ordered_candidates(sem_p)):
                return True
        return False

    MAX_ROUNDS = 50
    overall_progress = True
    rounds = 0
    while overall_progress and rounds < MAX_ROUNDS:
        overall_progress = False
        rounds += 1

        # Drain anything beyond the 4-year window, most-constrained first.
        max_sem = max(new_schedule.keys())
        stranded = [
            (late_sem, code)
            for late_sem in range(TARGET_SEMESTERS, max_sem + 1)
            for code in new_schedule.get(late_sem, [])
            if not (code.startswith("ELECTIVE:") or code.startswith("FAILED:"))
        ]
        stranded.sort(key=lambda item: count_options(item[1], _ordered_candidates(TARGET_SEMESTERS)))

        still_stranded = []
        for late_sem, code in stranded:
            if try_place(code, late_sem, _ordered_candidates(TARGET_SEMESTERS)):
                overall_progress = True
            else:
                still_stranded.append((late_sem, code))

        # Nothing placed directly this round - try nudging a blocking
        # prerequisite earlier for the most-constrained leftover unit.
        if not overall_progress:
            for late_sem, code in still_stranded:
                if nudge_blocker_earlier(code):
                    overall_progress = True
                    break

        # Top up anything still under MIN, pulling from whatever's later
        # (including any remainder beyond TARGET_SEMESTERS). Gap/intensive
        # positions are never top-up TARGETS - they're not expected to
        # carry a full course load, just an occasional single unit.
        for sem_index in sorted(new_schedule.keys()):
            if _position_kind(sem_index) not in ("sem1", "sem2"):
                continue
            while semester_credit_points(new_schedule, sem_index, merged_data) < MIN_CP_PER_SEMESTER:
                current_max_sem = max(new_schedule.keys())
                moved = False
                for later_sem in range(sem_index + 1, current_max_sem + 1):
                    for code in list(new_schedule.get(later_sem, [])):
                        if code.startswith("ELECTIVE:") or code.startswith("FAILED:"):
                            continue
                        if _can_move_here(code, sem_index, new_schedule, merged_data, prereqs, coreqs, cap=cap_for(sem_index)):
                            new_schedule[later_sem].remove(code)
                            new_schedule[sem_index].append(code)
                            moved = True
                            overall_progress = True
                            break
                    if moved:
                        break
                if not moved:
                    break

    return _prune_empty_semesters(new_schedule)


def rebalance_schedule(schedule, merged_data, prereqs, coreqs):
    """
    A full oversight pass: checks EVERY currently-scheduled real unit (not
    just units compact_schedule() happened to be actively placing) for
    whether it could legally move to an earlier semester than where it
    currently sits, and moves it if so - repeating full passes until one
    makes no further changes.

    This exists because compact_schedule() only ever reconsiders units it
    is actively trying to place: units stranded beyond the 4-year window,
    or a specific blocker identified by the nudge step. It never asks "is
    everything ELSE also sitting in its best spot?" - a unit that was
    never stranded and never got specifically nudged could still be
    sitting later than it needs to, quietly eating room a more
    constrained unit might need. This function checks that directly, for
    every unit, not just the ones that already caused a visible problem.

    "Better-fit" here means "as early as legally possible" - pushing
    every unit forward tends to free up later slots for whatever turns
    out to be most constrained, which is the same instinct behind
    compact_schedule()'s nudge step, generalised to the whole plan rather
    than only the specific unit currently blocking a stranded one.

    Uses the same tiered (non-overload preferred, then budgeted overload)
    and failure-proportional overload-budget logic as compact_schedule -
    see that function's docstring for what the budget means. Every move
    this function makes is safe in the sense that it can only ever HELP
    other units' constraints (moving a prerequisite earlier never breaks
    a "must be after" requirement, only satisfies it more easily) and
    only ever FREES room in the semester being left, never consumes room
    from an already-validated placement - so this pass cannot invalidate
    a schedule that was already internally consistent going in.

    FAILED: markers and elective placeholders are never touched (as
    elsewhere, a failed attempt is history, not something to reschedule).

    Units belonging to any "Specialisation - ..." block are ALSO never
    touched, even after assign_specialisation_electives() has replaced
    their placeholder with the real unit code. Reason: once a
    specialisation unit occupies a slot, the schedule itself has no
    memory of WHY that slot was chosen - only that it needed a
    stream-type or free-type option (see _pool_for_slot). A pool-blind
    move here could relocate it into a real semester slot of the wrong
    type, silently breaking the specialisation requirement it was placed
    there to satisfy. assign_specialisation_electives() already uses its
    own purpose-built, pool-aware, most-constrained-first logic to place
    these units correctly - this function leaves them exactly where that
    placed them, and only rebalances the units around them. Safe to call
    this as the final pass after assign_specialisation_electives() for
    exactly this reason.
    """
    new_schedule = {sem: list(codes) for sem, codes in schedule.items()}

    def _is_specialisation_unit(code):
        blocks = merged_data["units"].get(code, {}).get("degree_blocks") or []
        return any(b.startswith("Specialisation - ") for b in blocks)

    num_failures = sum(
        1 for codes in new_schedule.values() for c in codes if c.startswith("FAILED:")
    )

    def overloaded_semester_count():
        return sum(
            1 for sem in range(TARGET_SEMESTERS)
            if _position_kind(sem) in ("sem1", "sem2")
            and semester_credit_points(new_schedule, sem, merged_data) > MIN_CP_PER_SEMESTER
        )

    def cap_for(sem_index):
        if semester_credit_points(new_schedule, sem_index, merged_data) > MIN_CP_PER_SEMESTER:
            return MAX_CP_PER_SEMESTER
        if overloaded_semester_count() < num_failures:
            return MAX_CP_PER_SEMESTER
        return MIN_CP_PER_SEMESTER

    MAX_ROUNDS = 50
    rounds = 0
    improved = True
    while improved and rounds < MAX_ROUNDS:
        improved = False
        rounds += 1

        semester_of = _semester_of_map(new_schedule)
        # Visit in ascending current-semester order so a round settles
        # earlier units before reconsidering ones that started later.
        codes_in_order = [code for code, _ in sorted(semester_of.items(), key=lambda kv: kv[1])]

        for code in codes_in_order:
            semester_of = _semester_of_map(new_schedule)  # refresh - may have shifted this round
            if code not in semester_of:
                continue
            current_sem = semester_of[code]
            if current_sem == 0:
                continue  # nothing earlier exists
            if _is_specialisation_unit(code):
                continue  # placed by assign_specialisation_electives() - leave it exactly where its pool-aware logic put it

            earlier_target = None
            for target in _ordered_candidates(current_sem):
                if _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=MIN_CP_PER_SEMESTER):
                    earlier_target = target
                    break
            if earlier_target is None:
                for target in _ordered_candidates(current_sem):
                    if _can_move_here(code, target, new_schedule, merged_data, prereqs, coreqs, cap=cap_for(target)):
                        earlier_target = target
                        break

            if earlier_target is not None:
                new_schedule[current_sem].remove(code)
                new_schedule[earlier_target].append(code)
                improved = True

    return _prune_empty_semesters(new_schedule)


# Matches the "(Advanced)"/"(Adv)" parenthetical suffix specifically -
# NOT any unit whose subject name happens to contain the word "Advanced"
# without parentheses (e.g. "Advanced Machine Learning", "Randomised and
# Advanced Algorithms" are standalone units, not advanced versions of
# something else, and correctly don't match).
ADVANCED_UNIT_PATTERN = re.compile(r"\(Adv(?:anced)?\)", re.IGNORECASE)


def _is_advanced_unit(code, merged_data):
    """
    True if `code`'s name marks it as the advanced VERSION of a standard
    unit (e.g. "Mathematics 1A (Advanced)", "Data Structures & Algorithms
    (Adv)") - see ADVANCED_UNIT_PATTERN for what does and doesn't match.
    """
    name = merged_data["units"].get(code, {}).get("name") or ""
    return bool(ADVANCED_UNIT_PATTERN.search(name))


def find_advanced_alternatives(schedule, merged_data):
    """
    For every unit actually scheduled, finds whether an advanced VERSION
    of it exists - detected via _is_advanced_unit() plus a
    prohibition_codes link back to the scheduled unit (an advanced unit
    typically lists its standard counterpart as a prohibition, e.g.
    COMP2823's prohibition_codes include COMP2123). This system never
    auto-selects an advanced unit itself (see assign_specialisation_electives) -
    this is purely informational, for a "did you know" notice.

    Returns [(standard_code, standard_name, advanced_code, advanced_name), ...].
    """
    scheduled_codes = set(_semester_of_map(schedule).keys())
    found = []
    for adv_code, adv_unit in merged_data["units"].items():
        if not _is_advanced_unit(adv_code, merged_data):
            continue
        prohibits = set(adv_unit.get("prohibition_codes") or [])
        for standard_code in prohibits & scheduled_codes:
            standard_name = merged_data["units"].get(standard_code, {}).get("name", "Unknown unit")
            found.append((standard_code, standard_name, adv_code, adv_unit.get("name", "Unknown unit")))
    return found


def print_advanced_unit_notice(schedule, merged_data):
    """Prints a notice for every scheduled unit that has an advanced alternative available."""
    alternatives = find_advanced_alternatives(schedule, merged_data)
    if not alternatives:
        return
    print("=" * 70)
    print("[NOTICE] Advanced unit alternatives")
    print("=" * 70)
    print(
        "The units below have an advanced version available. This system "
        "only ever schedules the standard version automatically - if "
        "you're interested in an advanced alternative, you'll need "
        "departmental permission to enrol in it instead.\n"
    )
    for standard_code, standard_name, adv_code, adv_name in sorted(alternatives):
        print(f"  {standard_code} ({standard_name}) -> advanced alternative: {adv_code} ({adv_name})")
    print()


def _pool_for_slot(block_names_text):
    """
    Given an ELECTIVE: placeholder's block-names text (e.g. "Software
    Stream 3000+ Level Elective Units | Free Electives"), returns
    (has_stream_option, has_free_option) - whether this slot can be
    filled from the degree's stream-elective pool and/or its free-
    elective pool.

    A block name is treated as the free-elective type only if it's
    literally "Free Electives" - confirmed as CUSP's consistent name for
    this block across every degree page checked (Software Engineering,
    Mechanical Engineering with Space). Any OTHER block name listed is
    treated as some flavour of the degree's own stream-elective pool -
    this holds because CUSP never lists a "Specialisation - ..." block
    as a slot option directly; specialisations work by REPLACING what a
    stream-elective or free-elective slot draws from (see the Pathways
    section text: "a stream specialisation is a subset of stream
    electives", "a breadth specialisation... takes the place of your
    free electives"), not by appearing as a third listed option.
    """
    parts = [b.strip() for b in block_names_text.split("|") if b.strip()]
    has_free = any(b == "Free Electives" for b in parts)
    has_stream = any(b != "Free Electives" for b in parts)
    return has_stream, has_free


def assign_specialisation_electives(schedule, merged_data, prereqs, coreqs,
                                     stream_specialisation=None, breadth_specialisation=None):
    """
    Resolves elective placeholder slots into real, specific units for
    whichever specialisation(s) the student has chosen - generic across
    degrees/streams, driven entirely by merged_data["specialisations"]
    (stream vs breadth type + Core/Elective block mapping, from
    scrape_cusp_degree.py's parse_specialisations()) and
    merged_data["degree_blocks_meta"] (each block's own CP requirement).
    No specialisation name or block name is hardcoded here.

    Supports all 4 combinations described in the degree rules, simply as
    a consequence of which arguments are passed:
      - stream_specialisation + no breadth  -> stream spec resolved,
        Free Electives slots left as placeholders (choice not made)
      - stream_specialisation + breadth_specialisation -> both resolved
      - no stream + no breadth -> nothing resolved (unchanged schedule) -
        this is "Stream Electives (generic) + Free Electives", i.e. no
        specialisation chosen at all; those slots stay as placeholders
        exactly as before, since which generic elective a student picks
        is their choice, not something this system decides for them
      - no stream + breadth_specialisation -> only breadth resolved

    For a chosen specialisation: every unit in its CORE block is
    mandatory and gets placed (not a pool to choose from - the block's
    min_cp == max_cp by definition). Its ELECTIVE block IS a pool -
    units are placed in most-constrained-first order until the block's
    own max_cp is used.

    IMPORTANT - candidates from BOTH specialisations are placed together
    in most-constrained-first order, not one specialisation's units fully
    before the other's. Some elective slots are "flexible" - eligible for
    either the stream pool or the free/breadth pool (e.g. a slot listing
    both "Software Stream 3000+ Level Elective Units" and "Free
    Electives" as options). If the stream specialisation's units were all
    placed first regardless of how constrained they individually are, a
    flexible Semester-2-only stream unit could claim the ONLY
    Semester-2-compatible flexible slot in the whole schedule purely by
    running first in a fixed order, stranding a breadth-specialisation
    unit that also needed that exact slot and had no other option at
    all - confirmed to happen in practice before this fix (IoT's
    ELEC3506 claimed Year 1 Sem 2's flexible slot ahead of Humanitarian
    Engineering's CIVL5320, which needed that exact slot and nothing
    else). Sorting every candidate (both pools, core and elective units
    together) by how many legal slots it currently has, and placing the
    most constrained first, prevents a less-constrained unit from an
    arbitrarily-earlier-processed pool from stealing a slot a more-
    constrained unit from the other pool genuinely needs.

    A unit only goes into a slot whose listed options actually include
    that pool type (see _pool_for_slot) AND where its prerequisites,
    corequisites, immediate-succession rule, and semester offering are
    genuinely satisfied at that position - the same checks used
    everywhere else in this module, not a separate, looser standard.

    GAP-SLOT FALLBACK: if a unit has no legal REAL-semester elective
    placeholder anywhere (see find_valid_placement's Tier 1), this also
    tries placing it directly into a winter/summer intensive gap slot
    within the 4-year window (Tier 2) - filling an elective requirement
    via an intensive session this way is a real, USYD-sanctioned option,
    not just a fallback invented for scheduling convenience: confirmed
    directly against sydney.edu.au/students/change-study-load.html (the
    "at least 18cp in the semester itself, up to 6cp via a matching
    intensive session" pattern). A gap-slot placement has no existing
    placeholder to replace - the unit is inserted as a new entry at that
    position instead. A normal semester's existing elective placeholder
    is always tried first; the gap slot is only ever a last resort.

    KNOWN GAP: "Free Electives" itself (when no breadth specialisation
    is chosen) can't be resolved to specific units yet - Table S data
    (the actual pool of eligible free-elective units, scraped separately
    into table_s_*.json) hasn't been merged into merged_degree_data.json
    with validated prerequisite data the way Software Stream units have.
    Those slots correctly stay as placeholders rather than being guessed.

    KNOWN INTERACTION: compact_schedule()/rebalance_schedule() don't know
    about pool-type restrictions - if you replan/compact/rebalance AFTER
    calling this, a specialisation unit could in principle get moved
    into a differently-typed slot (or a gap-slot placement could get
    shuffled by the general load-balancing logic, which doesn't know it
    represents a specific elective requirement rather than an ordinary
    unit). Run this AFTER those passes, or re-validate afterward, rather
    than assuming it's safe to reorder.

    Returns (new_schedule, report) where report has "placed"
    ([(code, semester_index), ...]), "skipped_mandatory_no_room"
    (core-block units that couldn't be legally placed anywhere even with
    most-constrained-first ordering - a real problem worth investigating
    by hand, since core units are not optional; if this happens, check
    that unit's actual sessions_offered/prerequisite data, since a truly
    unsatisfiable case - like the earlier COMP3221 stress test - is a
    genuine finding, not necessarily a bug in this function), and
    "pool_completion" ({block_name: {"used_cp": int, "target_cp": int}}
    for each elective pool) - an elective pool with used_cp < target_cp
    is worth a human's attention, but NOT automatically confirmed as a
    real degree-structure gap either way.

    UNRESOLVED, FLAGGED HONESTLY (2026-08-27): every specialisation
    elective block's min_cp in this dataset reads None, which would mean
    "no stated minimum - fill the rest with ordinary electives, still
    graduates on time." But that min_cp value comes from
    stress_test.py's load_patched_data() - a hand-typed test-harness
    placeholder, NOT confirmed against USYD's real handbook page for any
    specific specialisation. Nobody has actually checked whether, e.g.,
    Engineering Data Science's Elective Units block is genuinely "up to
    12cp, take what's available" or "12cp required." A prior version of
    this docstring stated the "not a real gap" conclusion as settled
    fact based on that placeholder - that was overconfident and has been
    removed. Until the real page is checked, treat used_cp < target_cp
    as an open question, not a confirmed non-issue OR a confirmed bug.
    """
    new_schedule = {sem: list(codes) for sem, codes in schedule.items()}
    specialisations = {s["name"]: s for s in merged_data.get("specialisations", [])}
    blocks_meta = merged_data.get("degree_blocks_meta", {})

    def codes_in_block(block_name):
        if not block_name:
            return []
        return sorted(
            code for code, u in merged_data["units"].items()
            if block_name in (u.get("degree_blocks") or [])
            and not _is_advanced_unit(code, merged_data)
        )

    def resolve(spec_name, expected_type):
        if spec_name is None:
            return None
        spec = specialisations.get(spec_name)
        if spec is None:
            raise ValueError(
                f"'{spec_name}' is not a known specialisation. Known: {sorted(specialisations)}"
            )
        if spec["type"] != expected_type:
            raise ValueError(f"'{spec_name}' is a {spec['type']} specialisation, not {expected_type}.")
        return spec

    stream_spec = resolve(stream_specialisation, "stream")
    breadth_spec = resolve(breadth_specialisation, "breadth")

    def block_is_mandatory_all(block_name):
        """
        A "Core" block only means "take every listed unit" if those
        units actually fit within the block's own max_cp - true for 5 of
        the 6 specialisations checked directly against real data
        (Computer Engineering, Intelligent Information Engineering, IoT,
        Humanitarian Engineering, Innovation and Entrepreneurship: total
        credit points of listed units exactly equals the cap). Engineering
        Data Science is a genuine exception: its "Core" block lists 6
        units worth 36cp (DATA2001/DATA2901, DATA2002/DATA2902,
        STAT2011/STAT2911 - regular/advanced pairs) but caps at 18cp -
        it's actually a smaller pool (pick one from each pair), not
        "take everything", despite being labelled "Core". Detected here
        by comparing the sum of unit credit points against the block's
        own max_cp, rather than assumed by block name - assuming
        "Core = mandatory-all" universally was exactly the bug a full
        specialisation-matrix stress test caught.
        """
        codes = codes_in_block(block_name)
        total_cp = sum(merged_data["units"].get(c, {}).get("credit_points") or 0 for c in codes)
        cap = blocks_meta.get(block_name, {}).get("max_cp")
        return cap is None or total_cp <= cap

    # Build the full candidate list up front - both specialisations,
    # core and elective, so most-constrained-first ordering can consider
    # everything together rather than pool-by-pool.
    candidates = []
    pool_caps = {}
    if stream_spec:
        core_mandatory = block_is_mandatory_all(stream_spec["core_block"])
        for code in codes_in_block(stream_spec["core_block"]):
            candidates.append({"code": code, "wants_stream_slot": True, "mandatory": core_mandatory, "pool": stream_spec["core_block"]})
        if not core_mandatory:
            pool_caps[stream_spec["core_block"]] = blocks_meta.get(stream_spec["core_block"], {}).get("max_cp")
        for code in codes_in_block(stream_spec["elective_block"]):
            candidates.append({"code": code, "wants_stream_slot": True, "mandatory": False, "pool": stream_spec["elective_block"]})
        pool_caps[stream_spec["elective_block"]] = blocks_meta.get(stream_spec["elective_block"], {}).get("max_cp")
    if breadth_spec:
        core_mandatory = block_is_mandatory_all(breadth_spec["core_block"])
        for code in codes_in_block(breadth_spec["core_block"]):
            candidates.append({"code": code, "wants_stream_slot": False, "mandatory": core_mandatory, "pool": breadth_spec["core_block"]})
        if not core_mandatory:
            pool_caps[breadth_spec["core_block"]] = blocks_meta.get(breadth_spec["core_block"], {}).get("max_cp")
        for code in codes_in_block(breadth_spec["elective_block"]):
            candidates.append({"code": code, "wants_stream_slot": False, "mandatory": False, "pool": breadth_spec["elective_block"]})
        pool_caps[breadth_spec["elective_block"]] = blocks_meta.get(breadth_spec["elective_block"], {}).get("max_cp")

    pool_cp_used = defaultdict(int)
    already_used = set()
    placed = []
    skipped_mandatory = []

    def slot_matches(slot, wants_stream_slot):
        if not slot.startswith("ELECTIVE:"):
            return False
        has_stream, has_free = _pool_for_slot(slot[len("ELECTIVE:"):])
        return has_stream if wants_stream_slot else has_free

    def find_valid_placement(code, wants_stream_slot):
        """
        Returns (target, via_gap). Tier 1: any REAL semester with a
        matching existing elective placeholder (as before) - checked
        first, since a normal semester enrolment is always preferred.
        Tier 2 (only reached if tier 1 finds nothing): any GAP
        (intensive) position within TARGET_SEMESTERS where the unit is
        legally offered AND the slot is still empty. Filling a
        specialisation elective requirement via an intensive session is
        a real, USYD-sanctioned option when there's no room in a normal
        semester - confirmed directly against
        sydney.edu.au/students/change-study-load.html (the "at least
        18cp in the semester itself, up to 6cp via a matching intensive"
        pattern for a teaching period's total). via_gap=True means there
        was no existing placeholder to replace - the caller needs to
        create/append to that position instead of overwriting a slot
        string.

        At most ONE unit per intensive slot (checked directly - an
        occupied gap slot is skipped entirely, not room-checked by
        credit points the way a real semester is) - a real student can't
        take multiple units in one intensive session, and intensive
        slots are rare enough that a simple occupancy check is
        sufficient rather than a credit-point cap. Since this loop walks
        EVERY gap position across all 4 years in order, two units that
        both need (say) a winter-gap slot don't actually collide: the
        first one to be processed takes Year 1's winter gap, and the
        second one - finding that slot already taken - simply continues
        to Year 2's winter gap instead, as long as its own prerequisites
        are satisfied by then. No separate contention-resolution logic
        needed; recurring slots resolve this "for free."
        """
        semester_of = _semester_of_map(new_schedule)

        def is_flexible_free_slot(slot):
            has_stream, has_free = _pool_for_slot(slot[len("ELECTIVE:"):])
            return has_stream and has_free

        # When a breadth specialisation is also active, a slot offering a
        # "Free Electives" option is the ONLY kind a breadth candidate can
        # ever use (see _pool_for_slot - breadth always wants_stream_slot
        # =False, and only has_free slots satisfy that). A stream
        # candidate usually has pure stream-only slots it could use
        # instead, so it should exhaust those first and only fall back to
        # a flexible slot if it genuinely has no other option - otherwise
        # it can starve breadth's own placement later in this same call
        # even when it didn't actually need that specific slot. Confirmed
        # necessary by a real case: Engineering Data Science claiming
        # both of Year 3 Sem 1's flexible "3000+ | Free Electives" slots
        # left Humanitarian Engineering's elective pool with nowhere to
        # go, even though EDS had no strict need for those particular
        # slots over a pure-stream one sitting right next to them.
        prefer_pure_stream = wants_stream_slot and breadth_specialisation is not None
        flexible_passes = (False, True) if prefer_pure_stream else (True,)

        for allow_flexible in flexible_passes:
            for sem_index in sorted(new_schedule.keys()):
                for slot in new_schedule[sem_index]:
                    if not slot_matches(slot, wants_stream_slot):
                        continue
                    if not allow_flexible and is_flexible_free_slot(slot):
                        continue
                    if not _prereq_satisfied(code, sem_index, new_schedule, semester_of, prereqs, merged_data):
                        continue
                    if not _immediate_successor_satisfied(code, sem_index, semester_of, merged_data):
                        continue
                    if not _coreq_satisfied(code, sem_index, semester_of, coreqs):
                        continue
                    if not _prohibition_satisfied(code, semester_of, merged_data):
                        continue
                    if not unit_offered_in(code, sem_index, merged_data):
                        continue
                    return sem_index, False

        for sem_index in range(TARGET_SEMESTERS):
            if _position_kind(sem_index) not in ("winter_gap", "summer_gap"):
                continue
            if new_schedule.get(sem_index):
                continue  # already has a unit - at most one per intensive slot
            if not _prereq_satisfied(code, sem_index, new_schedule, semester_of, prereqs, merged_data):
                continue
            if not _immediate_successor_satisfied(code, sem_index, semester_of, merged_data):
                continue
            if not _coreq_satisfied(code, sem_index, semester_of, coreqs):
                continue
            if not _prohibition_satisfied(code, semester_of, merged_data):
                continue
            if not unit_offered_in(code, sem_index, merged_data):
                continue
            return sem_index, True

        return None, False

    def count_valid_options(cand):
        code = cand["code"]
        wants_stream_slot = cand["wants_stream_slot"]
        semester_of = _semester_of_map(new_schedule)
        count = 0
        for sem_index in sorted(new_schedule.keys()):
            for slot in new_schedule[sem_index]:
                if not slot_matches(slot, wants_stream_slot):
                    continue
                if not _prereq_satisfied(code, sem_index, new_schedule, semester_of, prereqs, merged_data):
                    continue
                if not _immediate_successor_satisfied(code, sem_index, semester_of, merged_data):
                    continue
                if not _coreq_satisfied(code, sem_index, semester_of, coreqs):
                    continue
                if not _prohibition_satisfied(code, semester_of, merged_data):
                    continue
                if not unit_offered_in(code, sem_index, merged_data):
                    continue
                count += 1
        for sem_index in range(TARGET_SEMESTERS):
            if _position_kind(sem_index) not in ("winter_gap", "summer_gap"):
                continue
            if new_schedule.get(sem_index):
                continue  # already occupied - not a real option, matches find_valid_placement
            if not _prereq_satisfied(code, sem_index, new_schedule, semester_of, prereqs, merged_data):
                continue
            if not _immediate_successor_satisfied(code, sem_index, semester_of, merged_data):
                continue
            if not _coreq_satisfied(code, sem_index, semester_of, coreqs):
                continue
            if not _prohibition_satisfied(code, semester_of, merged_data):
                continue
            if not unit_offered_in(code, sem_index, merged_data):
                continue
            count += 1
        return count

    remaining = candidates
    progress = True
    while progress and remaining:
        progress = False
        # Mandatory (core) candidates always sort ahead of elective ones -
        # a core unit is a real requirement, not a choice, so it should
        # never lose a slot to a more-flexible elective purely because
        # the elective happened to look more urgent by constraint count
        # alone. Most-constrained-first ordering still applies WITHIN
        # each tier (mandatory candidates among themselves, elective
        # candidates among themselves) - this is what prevented one
        # specialisation's flexible core from stealing a slot a more
        # constrained core (from either specialisation) genuinely needed
        # (see the ELEC3506/CIVL5320 contention fix). Only once a slot
        # genuinely can't take any remaining core unit does an elective
        # get a chance to use it, on this round or a later one.
        remaining.sort(key=lambda c: (0 if c["mandatory"] else 1, count_valid_options(c)))

        still = []
        for cand in remaining:
            code, wants_stream_slot = cand["code"], cand["wants_stream_slot"]
            if code in already_used:
                continue

            if not cand["mandatory"]:
                cap = pool_caps.get(cand["pool"])
                unit_cp = merged_data["units"].get(code, {}).get("credit_points") or 0
                if cap is not None and pool_cp_used[cand["pool"]] + unit_cp > cap:
                    continue  # exceeds this pool's own CP cap - drop, not mandatory

            target, via_gap = find_valid_placement(code, wants_stream_slot)
            if target is None:
                if cand["mandatory"]:
                    still.append(cand)
                continue

            if via_gap:
                new_schedule.setdefault(target, []).append(code)
            else:
                i = next(idx for idx, slot in enumerate(new_schedule[target]) if slot_matches(slot, wants_stream_slot))
                new_schedule[target][i] = code
            placed.append((code, target))
            already_used.add(code)
            if not cand["mandatory"]:
                unit_cp = merged_data["units"].get(code, {}).get("credit_points") or 0
                pool_cp_used[cand["pool"]] += unit_cp
            progress = True

        remaining = still

    skipped_mandatory = [c["code"] for c in remaining if c["mandatory"]]

    pool_completion = {
        pool: {"used_cp": pool_cp_used.get(pool, 0), "target_cp": cap}
        for pool, cap in pool_caps.items()
    }

    return new_schedule, {
        "placed": placed,
        "skipped_mandatory_no_room": skipped_mandatory,
        "pool_completion": pool_completion,
    }


def get_core_unit_codes(base_schedule):
    """
    Returns the set of unit codes that are genuine degree CORE units -
    captured from the schedule BEFORE any elective slot has been
    resolved (i.e. straight from parse_default_schedule(), or after
    compact_schedule()/rebalance_schedule() on that same base - never
    after assign_specialisation_electives()). Every code present at that
    point, other than the "ELECTIVE:..." placeholder strings themselves,
    is by construction a core requirement sitting exactly where USYD's
    official plan puts it - Software Stream Core, Introductory
    Computing, Foundation Mathematics, Projects Table, etc.

    This is the reference used to stop resolve_credit_thresholds() (and
    any future elective-reordering logic) from ever touching a core
    unit's position - only units that started as an elective
    placeholder, and were later resolved into a real code by
    assign_specialisation_electives(), are fair game to reorder.
    """
    return {
        code for codes in base_schedule.values() for code in codes
        if not code.startswith("ELECTIVE:") and not code.startswith("FAILED:")
    }


def resolve_credit_thresholds(schedule, merged_data, prereqs, coreqs, dependents, core_unit_codes, max_rounds=50):
    """
    Repairs any unmet credit_point_requirement (e.g. ELEC4712's "36cp of
    3000+ BE units") by SWAPPING already-scheduled units' positions, not
    by choosing different units - whatever assign_specialisation_electives()
    (or the base schedule) already chose is trusted as-is; this only
    reorders WHEN things happen.

    Why this exists: every legitimate specialisation combination is
    expected to be completable within the standard 4 years (confirmed
    directly - a manually-constructed Computer Engineering + Innovation
    and Entrepreneurship schedule hits ELEC4712's 36cp threshold exactly,
    proving a satisfying arrangement exists even though this system's own
    single-pass placement came up short for the same combination).
    assign_specialisation_electives()'s most-constrained-first placement
    has no awareness that a downstream credit threshold specifically
    wants 3000+-level content front-loaded - it optimises only for "don't
    strand a tightly-constrained unit", which is a different, sometimes
    conflicting goal. This pass repairs the gap between "a valid choice
    of units" and "a valid ORDERING of those units" afterward.

    THREE-TIER PRIORITY (2026-08-25 policy - never touch core by default,
    only escalate when an elective-only fix genuinely isn't enough):

      Tier 1 - relocate an unfilled ELECTIVE placeholder that already
      qualifies by its own label (e.g. a slot whose name contains
      "3000+" already counts toward an uncapped min-level requirement,
      per _credit_requirement_satisfied - see its docstring). Cheapest
      possible fix: no specific unit needs choosing, no prerequisites of
      its own to re-check, it just needs to be EARLIER. Found via a real
      case: DATA2002/STAT2011 taking up two of Engineering Data Science's
      elective slots left ELEC4712 6cp short - simply swapping STAT2011
      with an already-open, already-"3000+"-labelled placeholder futher
      along the schedule closed the gap exactly, with no new unit
      involved at all.

      Tier 2 - swap two already-scheduled ELECTIVE-ORIGIN units' positions
      (the original mechanism - unchanged, core is never a candidate on
      either side here).

      Tier 3 - LAST RESORT ONLY, tried in a second full pass and only if
      tiers 1-2 leave something genuinely unresolved after a full round of
      no progress: allow a CORE unit as the swap's HELPER (never as the
      blocker, and never chosen over an elective-only fix that already
      works - see the loop below, which only unlocks this after tiers 1-2
      have been exhausted). Even then, only a core unit that ISN'T a
      prerequisite or corequisite of anything else currently scheduled
      anywhere in the plan (checked via `dependents`, not just "would
      this technically break" - see the ELEC1005 story below for why that
      distinction matters) - so no other unit's own progression is put at
      risk by moving it.

    CORE UNITS ARE NEVER MOVED BY DEFAULT - not "moved only if safe", not
    considered as a candidate at all in tiers 1-2. This is the primary
    safety mechanism, not a secondary check: core_unit_codes (see
    get_core_unit_codes) identifies every unit that was placed by USYD's
    own official plan, before any elective slot was ever resolved -
    Software Stream Core, Introductory Computing, Foundation Mathematics,
    Projects Table, etc. A first version of this function only checked
    "would moving this unit break something that depends on it", which is
    necessary but not sufficient - it swapped ELEC1005 (a core Projects
    Table unit nothing else happens to formally depend on) all the way to
    Year 4, satisfying the threshold's numbers while producing a schedule
    that no longer matched the degree's own official placement for a
    mandatory unit. The rule is about WHAT kind of unit this is, not just
    what would technically break - which is why Tier 3's core check is
    "not a prerequisite/corequisite of anything scheduled", not merely
    "moving it wouldn't break anything" (moving a helper EARLIER never
    breaks its own dependents anyway - see move_breaks_dependents' own
    note on this - so a weaker check would let almost any core unit
    through; the stricter one is deliberate).


    For each unit with an unmet threshold, looks for an ELECTIVE-ORIGIN
    unit already scheduled AT OR AFTER it that would help (matching
    level/prefix, not excluded) and swaps its position with another
    ELECTIVE-ORIGIN unit scheduled BEFORE the threshold unit - only if
    the swap is legal for BOTH units in their new positions
    (prerequisites, corequisites, prohibitions, offering) AND doesn't
    break any OTHER already-scheduled unit that currently relies on the
    displaced ("blocker") unit as a satisfied prerequisite or
    corequisite (see move_breaks_dependents).

    CORE UNITS ARE NEVER MOVED, PERIOD - not "moved only if safe", never
    considered as a candidate at all, on either side of a swap. This is
    the primary safety mechanism, not a secondary check: core_unit_codes
    (see get_core_unit_codes) identifies every unit that was placed by
    USYD's own official plan, before any elective slot was ever
    resolved - Software Stream Core, Introductory Computing, Foundation
    Mathematics, Projects Table, etc. A first version of this function
    only checked "would moving this unit break something that depends on
    it", which is necessary but not sufficient - it swapped ELEC1005 (a
    core Projects Table unit nothing else happens to formally depend on)
    all the way to Year 4, satisfying the threshold's numbers while
    producing a schedule that no longer matches the degree's own
    official placement for a mandatory unit. The rule is about WHAT kind
    of unit this is, not just what would technically break: only a unit
    that started as an "ELECTIVE:..." placeholder and was later resolved
    by assign_specialisation_electives() is fair game to reorder here.
    The move_breaks_dependents check below still applies ON TOP of this,
    since two elective-origin units can still have a formal dependency
    on each other.

    SEQUENCING: this function belongs to the "decide the electives"
    phase, run once while building a student's personalised baseline
    schedule (core + chosen electives, fully resolved and validated) -
    not intermixed with failure recovery. Once that baseline exists,
    replan_after_failure()/compact_schedule()/rebalance_schedule() are a
    separate, later phase that operates on top of it and DOES legitimately
    move core units when a real failure requires it - that's a different
    problem (recovering from something that actually happened) from this
    one (choosing a good arrangement of electives before anything has
    happened yet).

    Moving the "helper" unit EARLIER never has the dependents problem - a
    prerequisite being satisfied sooner can only help anything depending
    on it, never break it - so only the blocker's move needs that check.

    Room is never a concern for the swap itself, since every unit in
    this degree is a uniform 6cp - swapping two positions never changes
    either semester's total. Repeats in rounds across every under-
    threshold unit until either everything is satisfied or a full round
    makes no further swaps - see report_unmet_credit_thresholds() for
    surfacing what's left, rather than silently accepting a shortfall.
    """
    new_schedule = {sem: list(codes) for sem, codes in schedule.items()}

    def move_breaks_dependents(moved_code, new_sem, semester_of):
        """
        True if moving `moved_code` to `new_sem` would break any OTHER
        already-scheduled unit that currently relies on it as a
        satisfied prerequisite or corequisite alternative - checked by
        simulating the move in a copy of semester_of and re-checking
        every real dependent's own requirement against that hypothetical
        state, not just moved_code's own requirement at its new spot.
        """
        hypothetical = dict(semester_of)
        hypothetical[moved_code] = new_sem
        for dep_code in dependents.get(moved_code, set()):
            if dep_code not in semester_of:
                continue
            dep_sem = semester_of[dep_code]
            if not _prereq_satisfied(dep_code, dep_sem, new_schedule, hypothetical, prereqs, merged_data):
                return True
            if not _coreq_satisfied(dep_code, dep_sem, hypothetical, coreqs):
                return True
        return False

    def find_swap(threshold_code, target_sem, allow_core):
        req = merged_data["units"].get(threshold_code, {}).get("credit_point_requirement")
        if not req:
            return None
        semester_of = _semester_of_map(new_schedule)
        excluded_patterns = [re.compile(p) for p in req.get("excluded_patterns", [])]

        def blocker_candidates():
            for blocker_sem in range(target_sem):
                for blocker_code in list(new_schedule.get(blocker_sem, [])):
                    if blocker_code.startswith("ELECTIVE:") or blocker_code.startswith("FAILED:"):
                        continue
                    if blocker_code in core_unit_codes:
                        continue  # the BLOCKER is never core, in any tier -
                                  # only tier 3's helper side may be
                    yield blocker_code, blocker_sem

        def blocker_move_ok(blocker_code, helper_sem):
            if not (
                _prereq_satisfied(blocker_code, helper_sem, new_schedule, semester_of, prereqs, merged_data)
                and _coreq_satisfied(blocker_code, helper_sem, semester_of, coreqs)
                and _prohibition_satisfied(blocker_code, semester_of, merged_data)
                and unit_offered_in(blocker_code, helper_sem, merged_data)
            ):
                return False
            return not move_breaks_dependents(blocker_code, helper_sem, semester_of)

        # Tier 1 - relocate an unfilled placeholder that already qualifies
        # by its own label (only meaningful for a plain "at least this
        # level" requirement - a bounded range or prefix restriction can't
        # be confirmed from a generic placeholder's name, same limitation
        # _credit_requirement_satisfied itself documents).
        if req.get("min_level") is not None and req.get("max_level") is None and not req.get("prefix"):
            level_tag = f"{req['min_level'] // 1000}000+"
            for helper_sem in sorted(new_schedule.keys()):
                if helper_sem < target_sem:
                    continue
                for slot in list(new_schedule[helper_sem]):
                    if not slot.startswith("ELECTIVE:") or level_tag not in slot:
                        continue
                    for blocker_code, blocker_sem in blocker_candidates():
                        if blocker_move_ok(blocker_code, helper_sem):
                            return slot, helper_sem, blocker_code, blocker_sem

        # Tier 2 - swap two already-scheduled ELECTIVE-ORIGIN units
        # (original mechanism, unchanged).
        for helper_code, helper_sem in sorted(semester_of.items(), key=lambda kv: kv[1]):
            if helper_sem < target_sem or helper_code == threshold_code:
                continue
            if helper_code in core_unit_codes:
                continue  # never move a core unit in tier 2, regardless of whether it'd technically be safe
            level = _unit_level(helper_code)
            if level is None:
                continue
            if req.get("min_level") is not None and level < req["min_level"]:
                continue
            if req.get("max_level") is not None and level > req["max_level"]:
                continue
            if req.get("prefix") and not helper_code.startswith(req["prefix"]):
                continue
            if any(p.fullmatch(helper_code) for p in excluded_patterns):
                continue
            for blocker_code, blocker_sem in blocker_candidates():
                if blocker_code == helper_code:
                    continue
                if not blocker_move_ok(blocker_code, helper_sem):
                    continue
                if not (
                    _prereq_satisfied(helper_code, blocker_sem, new_schedule, semester_of, prereqs, merged_data)
                    and _coreq_satisfied(helper_code, blocker_sem, semester_of, coreqs)
                    and _prohibition_satisfied(helper_code, semester_of, merged_data)
                    and unit_offered_in(helper_code, blocker_sem, merged_data)
                ):
                    continue
                return helper_code, helper_sem, blocker_code, blocker_sem

        # Tier 3 - LAST RESORT, only reachable when the caller has already
        # confirmed tiers 1-2 leave something unresolved (see the two-pass
        # loop below). A core unit may serve as the HELPER only - never
        # the blocker (blocker_candidates() already excludes core) - and
        # only if nothing currently scheduled anywhere in the plan relies
        # on it as a prerequisite or corequisite, so no other unit's own
        # progression is put at risk by moving it.
        if allow_core:
            for helper_code, helper_sem in sorted(semester_of.items(), key=lambda kv: kv[1]):
                if helper_sem < target_sem or helper_code == threshold_code:
                    continue
                if helper_code not in core_unit_codes:
                    continue  # non-core already tried in tier 2
                if any(dep in semester_of for dep in dependents.get(helper_code, set())):
                    continue  # only a core unit NOTHING scheduled depends on may move
                level = _unit_level(helper_code)
                if level is None:
                    continue
                if req.get("min_level") is not None and level < req["min_level"]:
                    continue
                if req.get("max_level") is not None and level > req["max_level"]:
                    continue
                if req.get("prefix") and not helper_code.startswith(req["prefix"]):
                    continue
                if any(p.fullmatch(helper_code) for p in excluded_patterns):
                    continue
                for blocker_code, blocker_sem in blocker_candidates():
                    if blocker_code == helper_code:
                        continue
                    if not blocker_move_ok(blocker_code, helper_sem):
                        continue
                    if not (
                        _prereq_satisfied(helper_code, blocker_sem, new_schedule, semester_of, prereqs, merged_data)
                        and _coreq_satisfied(helper_code, blocker_sem, semester_of, coreqs)
                        and _prohibition_satisfied(helper_code, semester_of, merged_data)
                        and unit_offered_in(helper_code, blocker_sem, merged_data)
                    ):
                        continue
                    return helper_code, helper_sem, blocker_code, blocker_sem
        return None

    rounds = 0
    progress = True
    while progress and rounds < max_rounds:
        progress = False
        rounds += 1
        semester_of = _semester_of_map(new_schedule)
        for code, sem_index in list(semester_of.items()):
            req = merged_data["units"].get(code, {}).get("credit_point_requirement")
            if not req:
                continue
            if _credit_requirement_satisfied(code, sem_index, new_schedule, merged_data):
                continue
            swap = find_swap(code, sem_index, allow_core=False)
            if swap is None:
                continue
            helper_code, helper_sem, blocker_code, blocker_sem = swap
            new_schedule[helper_sem].remove(helper_code)
            new_schedule[blocker_sem].remove(blocker_code)
            new_schedule[helper_sem].append(blocker_code)
            new_schedule[blocker_sem].append(helper_code)
            progress = True

    # Tier 3 fallback pass - only runs at all if something is still unmet
    # after tiers 1-2 have fully run out of moves to make (progress
    # stopped above with unresolved thresholds still remaining), never
    # instead of a working elective-only fix.
    still_unmet = any(
        merged_data["units"].get(code, {}).get("credit_point_requirement")
        and not _credit_requirement_satisfied(code, sem_index, new_schedule, merged_data)
        for code, sem_index in _semester_of_map(new_schedule).items()
    )
    if still_unmet:
        rounds = 0
        progress = True
        while progress and rounds < max_rounds:
            progress = False
            rounds += 1
            semester_of = _semester_of_map(new_schedule)
            for code, sem_index in list(semester_of.items()):
                req = merged_data["units"].get(code, {}).get("credit_point_requirement")
                if not req:
                    continue
                if _credit_requirement_satisfied(code, sem_index, new_schedule, merged_data):
                    continue
                swap = find_swap(code, sem_index, allow_core=True)
                if swap is None:
                    continue
                helper_code, helper_sem, blocker_code, blocker_sem = swap
                new_schedule[helper_sem].remove(helper_code)
                new_schedule[blocker_sem].remove(blocker_code)
                new_schedule[helper_sem].append(blocker_code)
                new_schedule[blocker_sem].append(helper_code)
                progress = True

    return new_schedule


def report_unmet_credit_thresholds(schedule, merged_data):
    """
    Returns [(code, sem_index, description), ...] for any unit whose
    credit_point_requirement still isn't satisfied after
    resolve_credit_thresholds() has run - expected to be empty given
    every legitimate combination should be satisfiable; reported
    explicitly rather than silently accepted if it isn't.
    """
    semester_of = _semester_of_map(schedule)
    unmet = []
    for code, sem_index in semester_of.items():
        req = merged_data["units"].get(code, {}).get("credit_point_requirement")
        if not req:
            continue
        if not _credit_requirement_satisfied(code, sem_index, schedule, merged_data):
            unmet.append((code, sem_index, f"needs {req['min_credit_points']}cp of matching units scheduled earlier"))
    return unmet


def report_underloaded_semesters(schedule, merged_data):
    """
    Returns [(semester_index, credit_points), ...] for any REAL semester
    still under MIN_CP_PER_SEMESTER after compaction - i.e. genuinely
    not enough eligible units were available to reach the target. Gap
    (intensive) positions are never flagged here - they're not expected
    to carry a full course load, just an occasional single unit.
    """
    return [
        (sem_index, semester_credit_points(schedule, sem_index, merged_data))
        for sem_index in sorted(schedule)
        if _position_kind(sem_index) in ("sem1", "sem2")
        and semester_credit_points(schedule, sem_index, merged_data) < MIN_CP_PER_SEMESTER
    ]


def print_schedule(schedule, merged_data):
    for sem_index in sorted(schedule):
        codes = schedule[sem_index]
        total_cp = semester_credit_points(schedule, sem_index, merged_data)
        print(f"{_position_label(sem_index)} ({total_cp}cp):")
        for code in codes:
            if code.startswith("ELECTIVE:"):
                print(f"  - [elective slot] {code[len('ELECTIVE:'):]}")
            elif code.startswith("FAILED:"):
                base = _base_code(code)
                name = merged_data["units"].get(base, {}).get("name", "Unknown unit")
                print(f"  - {base}: {name}  [FAILED - retaken later]")
            else:
                name = merged_data["units"].get(code, {}).get("name", "Unknown unit")
                print(f"  - {code}: {name}")
        print()


def simulate_failure(label, schedule, failed_code, merged_data, prereqs, dependents, coreqs):
    """
    Runs one failure->replan->compact->validate cycle and prints the
    result. Returns the resulting schedule (so a second failure can be
    simulated on top of it, e.g. the retake also fails), or None if
    replanning hit the MAX_SEARCH_HORIZON_SEMESTERS backstop.
    """
    print(f"=== {label}: student fails {failed_code} - replanning ===\n")
    try:
        replanned = replan_after_failure(schedule, failed_code, merged_data, prereqs, dependents, coreqs)
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return None
    replanned = compact_schedule(replanned, merged_data, prereqs, coreqs)
    replanned = rebalance_schedule(replanned, merged_data, prereqs, coreqs)
    print_schedule(replanned, merged_data)

    underloaded = report_underloaded_semesters(replanned, merged_data)
    if underloaded:
        print("[INFO] Semesters below the 24cp target even after compaction:")
        for sem_index, cp in underloaded:
            print(f"  - {_position_label(sem_index)}: {cp}cp")
        print()

    violations = validate_schedule(replanned, prereqs, coreqs, merged_data)
    if violations:
        print("[WARN] Replanned schedule still has violations:")
        for v in violations:
            print(f"  - {v}")
    else:
        print("Replanned schedule is internally consistent.")
    print()

    return replanned


def main():
    merged_data = load_merged_data()
    prereqs, coreqs, dependents = build_prereq_graph(merged_data)

    schedule = parse_default_schedule(merged_data)
    schedule = compact_schedule(schedule, merged_data, prereqs, coreqs)
    schedule = rebalance_schedule(schedule, merged_data, prereqs, coreqs)
    print("=== Default schedule (USYD's official plan, load-balanced to 24-30cp) ===\n")
    print_schedule(schedule, merged_data)

    underloaded = report_underloaded_semesters(schedule, merged_data)
    if underloaded:
        print("[INFO] Semesters below the 24cp target even after compaction "
              "(not enough eligible units left to move forward, not a bug):")
        for sem_index, cp in underloaded:
            print(f"  - {_position_label(sem_index)}: {cp}cp")
        print()

    violations = validate_schedule(schedule, prereqs, coreqs, merged_data)
    if violations:
        print("=== Validation: issues found in the default plan ===")
        for v in violations:
            print(f"  - {v}")
    else:
        print("=== Validation: default plan is internally consistent with requirement data ===")
    print()

    print_pep_notice(merged_data)
    print_advanced_unit_notice(schedule, merged_data)

    DEMO_FAILED_UNIT = "INFO1113"
    scheduled_codes = {c for codes in schedule.values() for c in codes}
    if DEMO_FAILED_UNIT in scheduled_codes:
        once_failed = simulate_failure(
            "Demo, attempt 1 fails", schedule, DEMO_FAILED_UNIT, merged_data, prereqs, dependents, coreqs
        )
        if once_failed is not None:
            # The retake ALSO fails - second failure applied on top of the
            # already-replanned schedule. replan_after_failure() finds the
            # retake (the plain, non-FAILED: entry) wherever compaction
            # left it and marks that as failed too, so the schedule ends
            # up with two separate FAILED:INFO1113 markers in two
            # different semesters, and cascades the dependents again from
            # the new failure point.
            simulate_failure(
                "Demo, attempt 2 (the retake) also fails", once_failed, DEMO_FAILED_UNIT,
                merged_data, prereqs, dependents, coreqs
            )
    else:
        print(f"[INFO] {DEMO_FAILED_UNIT} isn't in the default schedule - "
              f"pick a different DEMO_FAILED_UNIT to see the replan demo.")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. Run merge_degree_data.py first if you haven't already - this script
#    depends on merged_degree_data.json existing. Table S data is NOT
#    required for this to work correctly.
# 2. MIN_CP_PER_SEMESTER=24 / MAX_CP_PER_SEMESTER=30 / TARGET_SEMESTERS=8
#    are placeholder assumptions (standard full-time load, 4-year target)
#    - worth confirming against the actual degree resolutions if you want
#    these to be authoritative rather than a reasonable default.
# 3. compact_schedule() is a single-pass greedy heuristic, not a globally
#    optimal rebalance - see its docstring for what it does and doesn't
#    guarantee.
# 4. Corequisite direction assumption: coreqs are treated as "same
#    semester or earlier", not strictly "must be exactly this semester" -
#    flag if that's not the intended university rule.
# 5. credit_point_requirement validation only applies to units that have
#    one explicitly attached in merge_degree_data.py's
#    MANUAL_UNIT_OVERRIDES / CREDIT_REQUIREMENT_PATCHES (currently
#    ELEC4712, MATH2069). Any other unit with a similar wildcard
#    requirement not yet identified and patched in still won't be
#    validated.