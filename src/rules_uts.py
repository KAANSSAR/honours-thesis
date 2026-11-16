"""
UTS rules: how C09066 answers the solver's questions.

Written against the dataset reparse_uts2.py produces, which differs from the USYD
one in ways that are easy to get wrong:

  - Codes are five bare digits, 48024, with no letters and no level encoded. The
    original planner's `[A-Z]{2,5}\\d{3,4}` could not match a single UTS subject.
  - Sessions are Autumn and Spring, with December and July short sessions, not
    Semester 1 and Semester 2.
  - Requisites are typed: an alternative may be a subject, a credit-point
    threshold, or text that cannot be checked at all. A requirement is satisfied
    if ANY of its alternatives holds. Flattening that to a code list is what made
    41029 look broken in all seven majors.
  - Corequisites are marked by a lowercase "c" on the code inside the ordinary
    requisite clause, and arrive as OR-groups. 41066 has seven alternatives, one of
    which suffices.
  - Some requisites may be waived depending on the student's course, which the
    handbook states outright. 31269 asks for 18cp of completed study while the
    Software sequence places it in Year 1 Autumn, and only the waiver note
    reconciles those.
  - Zero-credit subjects exist. 41028 Engineering Work Experience is 0cp, so a
    missing credit-point value must not be conflated with a genuine zero.

UTS needs no elective assignment and no credit-threshold repair. Its default plans
arrive with their published choice groups already resolved and their 24cp elective
allowance deliberately left open for the planner, so both optional strategies
return None and the solver skips those steps rather than running a stub.
"""
import re

import rules
from rules import POSITIONS_PER_YEAR, POS_SEM1, POS_SEM2, base_code, position_kind

# A full-time load is 24cp, but the published sequences run to 27cp wherever a 3cp
# professional-practice subject sits alongside four 6cp subjects, so 27 is a
# correct load rather than an overload. MAX allows headroom above that for
# compaction, on the same reasoning as USYD's.
MIN_CP = 24
MAX_CP = 30

CODE_PATTERN = re.compile(r"\b(\d{5})\b")

# Session name to position kind. December sits after Spring in the same year and
# July between Autumn and Spring, so each maps to the gap that follows the
# teaching period it trails.
SESSION_KIND = {
    "autumn": "sem1",
    "spring": "sem2",
    "july": "winter_gap",
    "winter": "winter_gap",
    "december": "summer_gap",
    "january": "summer_gap",
    "summer": "summer_gap",
}
SESSION_WORD = re.compile(
    r"\b(Autumn|Spring|Summer|Winter|December|January|July)\b", re.IGNORECASE)


def session_kinds(sessions):
    """The position kinds a unit's sessions_offered list covers."""
    found = set()
    for s in sessions or []:
        for word in SESSION_WORD.findall(s):
            kind = SESSION_KIND.get(word.lower())
            if kind:
                found.add(kind)
    return found


class UTSRules(rules.Rules):
    institution = "UTS"
    min_cp_per_period = MIN_CP
    max_cp_per_period = MAX_CP
    elective_slot_cp = 6
    code_pattern = CODE_PATTERN

    # ---- identity --------------------------------------------------------

    def unit_level(self, code):
        """
        None, always. UTS codes carry no level: 48024 is second-year programming
        and 33130 is first-year mathematics, and nothing in either code says so.
        Any check that needs a level has to come from the sequence position
        instead, which is why no UTS requisite in the dataset uses one.
        """
        return None

    def position_label(self, index):
        year = rules.year_of(index)
        kind = position_kind(index)
        if kind == "sem1":
            return f"Year {year} - Autumn session"
        if kind == "sem2":
            return f"Year {year} - Spring session"
        if kind == "winter_gap":
            return f"Year {year} - July session"
        return f"Year {year} - December session"

    # ---- availability ----------------------------------------------------

    def offered_in(self, code, index, data):
        """
        Availability comes from the handbook's "Typical availability" field, which
        is advisory rather than a guarantee. Modelling it as a hard constraint
        makes this engine stricter than its source, and that is a deliberate
        choice: six placements in the published sequences fall outside it, and
        reporting those is more useful than silently permitting anything.

        Missing availability returns True for a teaching period, matching the USYD
        treatment, rather than blocking on data that cannot resolve. A gap position
        still requires explicit confirmation, since short sessions are rare.
        """
        unit = data["units"].get(base_code(code), {})
        sessions = unit.get("sessions_offered")
        kind = position_kind(index)

        if kind in rules.GAP_KINDS:
            return bool(sessions) and kind in session_kinds(sessions)
        if not sessions:
            return True
        return kind in session_kinds(sessions)

    def period_owner(self, gap_index, code, data):
        """
        Which teaching period a short-session placement's load counts against.
        UTS draws no administrative distinction between teaching periods the way
        USYD's S1 and S2 intensive prefixes do, so this follows the calendar: a
        July session sits between Autumn and Spring and a December session after
        Spring, and both are charged to that year's Spring.
        """
        kind = position_kind(gap_index)
        if kind not in rules.GAP_KINDS:
            return None
        year = gap_index // POSITIONS_PER_YEAR
        return year * POSITIONS_PER_YEAR + POS_SEM2

    # ---- requisites ------------------------------------------------------

    def _alternative_met(self, alt, target, semester_of, before_cp):
        if alt.get("kind") == "subject":
            c = alt.get("code")
            return c in semester_of and semester_of[c] < target
        if alt.get("kind") == "credit_points":
            return before_cp >= alt.get("min_credit_points", 0)
        # Text the planner cannot check, such as "admission to a named course".
        # Treated as met so it does not fail an otherwise sound plan; it is
        # reported separately via unmodelled_requisite_text instead of being
        # silently dropped or silently enforced.
        return True

    def _credit_before(self, target, schedule, data):
        """
        Credit points completed strictly before a position, open elective slots
        included. Those are credit the student will take, merely not yet chosen,
        and leaving them out understates every total by up to 24cp.
        """
        total = 0
        for index, codes in schedule.items():
            if index >= target:
                continue
            for c in codes:
                if c.startswith(rules.FAILED_PREFIX):
                    continue
                total += self.unit_cp(c, data)
        return total

    def prereq_satisfied(self, code, target, schedule, semester_of, prereqs, data):
        """
        An AND of ORs over typed alternatives. Falls back to the flat code list
        only where the typed form is absent, so a dataset built before the typed
        fields existed still works rather than silently passing everything.
        """
        unit = data["units"].get(base_code(code), {})
        if unit.get("requisite_waived_for_some_courses"):
            return True

        requirements = unit.get("prerequisite_requirements")
        if requirements:
            before_cp = self._credit_before(target, schedule, data)
            return all(
                any(self._alternative_met(a, target, semester_of, before_cp)
                    for a in req)
                for req in requirements)

        required = prereqs.get(code, set())
        if required:
            return any(p in semester_of and semester_of[p] < target for p in required)
        return True

    def describe_prereq(self, code, data):
        """
        The prerequisite in words, read from the same fields `prereq_satisfied`
        reads and in the same precedence order.

        This exists so that anything which has to state the rule, rather than
        check it, cannot state a different rule. The baseline of
        Section~4.5.5 rendered `prerequisite_groups` instead, which is a derived
        field that keeps only the subject alternatives: on UTS Software that
        dropped the credit-point alternative from eight requirements and left
        one of them, 41029, reading as though it needed 41048, a subject the
        plan does not contain and the model could not have scheduled. The engine
        was right and the prompt was wrong, which is the more dangerous way round
        for a comparison to be broken, since it understates the baseline.
        """
        unit = data["units"].get(base_code(code), {})
        if unit.get("requisite_waived_for_some_courses"):
            return None

        requirements = unit.get("prerequisite_requirements")
        if requirements:
            groups = []
            for req in requirements:
                parts = []
                for alt in req:
                    if alt.get("kind") == "subject":
                        rendered = str(alt.get("code"))
                    elif alt.get("kind") == "credit_points":
                        rendered = f"{alt.get('min_credit_points', 0)}cp completed"
                    else:
                        # Anything the planner cannot check is treated as met by
                        # `_alternative_met`, so saying it here would describe a
                        # requirement that is not enforced.
                        continue
                    # The source lists one alternative per phrasing, so the same
                    # alternative recurs: 41127 carries the 72 credit point rule
                    # six times. `any` does not care and a reader does, and a
                    # requirement repeated six times reads like six conditions.
                    if rendered not in parts:
                        parts.append(rendered)
                if parts:
                    groups.append("(" + " or ".join(parts) + ")")
            return " AND ".join(groups) if groups else None

        required = sorted(set(unit.get("prerequisite_codes") or []))
        return "(" + " or ".join(required) + ")" if required else None

    def describe_coreq(self, code, data):
        """
        The corequisite in words, from the same fields `coreq_satisfied` reads.

        Added for the same reason as `describe_prereq`: the baseline prompt
        stated prerequisites and availability and nothing else, while the
        validator also enforces corequisites. On UTS that happened to cost
        nothing, no subject in scope having one, but it is the same defect and
        is closed in the same place.
        """
        unit = data["units"].get(base_code(code), {})
        groups = unit.get("corequisite_requirements")
        if groups:
            out = []
            for group in groups:
                parts = []
                for alt in group:
                    c = alt.get("code")
                    if c and c not in parts:
                        parts.append(str(c))
                if parts:
                    out.append("(" + " or ".join(parts) + ")")
            return " AND ".join(out) if out else None

        required = sorted(set(unit.get("corequisite_codes") or []))
        return "(" + " or ".join(required) + ")" if required else None

    def coreq_satisfied(self, code, target, semester_of, coreqs, data):
        """
        OR within each requirement, satisfied concurrently or earlier. Uses the
        grouped form where present: 41066 lists seven "c"-marked alternatives and
        needs one, not all seven.
        """
        unit = data["units"].get(base_code(code), {})
        groups = unit.get("corequisite_requirements")
        if groups:
            return all(
                any(a.get("code") in semester_of
                    and semester_of[a["code"]] <= target for a in group)
                for group in groups)

        required = coreqs.get(code, set())
        if not required:
            return True
        return any(c in semester_of and semester_of[c] <= target for c in required)

    def exclusion_satisfied(self, code, semester_of, data):
        """
        Anti-requisites, UTS's term for mutual exclusion. Checked both ways for the
        same reason as USYD prohibitions: the pages are not symmetric.
        """
        own = set(data["units"].get(base_code(code), {}).get("antirequisite_codes") or [])
        for other in semester_of:
            if other == code:
                continue
            if other in own:
                return False
            other_rec = data["units"].get(base_code(other), {})
            if code in set(other_rec.get("antirequisite_codes") or []):
                return False
        return True

    # ---- credit points ---------------------------------------------------

    def unit_cp(self, code, data):
        """
        A missing credit-point value and a genuine zero are different things here.
        41028 Engineering Work Experience really is 0cp and every major requires
        it, so defaulting a missing value to zero would hide a data gap behind a
        plausible number. Unknown falls back to 6, the standard subject size, and
        an explicit 0 is respected.
        """
        if code.startswith(rules.ELECTIVE_PREFIX):
            return self.elective_slot_cp
        cp = data["units"].get(base_code(code), {}).get("credit_points")
        return 6 if cp is None else cp

    # ---- optional strategies ---------------------------------------------

    def elective_assigner(self):
        """
        None. The published choice groups are resolved in uts_plan_picks.json when
        the default plans are built, and the 24cp elective allowance is left open
        on purpose for the planner to fill.
        """
        return None

    def threshold_resolver(self):
        """None. No UTS requisite in the dataset needs threshold repair."""
        return None
