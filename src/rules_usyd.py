"""
USYD rules: how a Sydney engineering degree answers the solver's questions.

Lifted from curriculum_planner.py with behaviour held fixed. Nothing here is
redesigned, because the 42 USYD default plans currently validate and the point of
the refactor is to put them behind an interface, not to change what they do. Where
a comment explains a decision that was reached the hard way, it is kept, since
those are the parts most likely to be broken by a well-meaning tidy-up.

Two assumptions in the original were genuinely USYD-only and are now stated as
such rather than being module constants everyone reads:

  - a unit code is letters then digits, INFO1110, and its level is the first digit
  - the teaching year has Semester 1 and Semester 2, with intensive sessions named
    "Int January", "Int June-July" and so on
"""
import re

import rules
from rules import (POSITIONS_PER_YEAR, POS_SEM1, POS_SEM2, base_code,
                  position_kind, previous_real_index)

# USYD full-time engineering load is 4 x 6cp. MAX allows one overload unit so
# compaction can prefer finishing in four years over spilling into a fifth.
MIN_CP = 24
MAX_CP = 30

CODE_PATTERN = re.compile(r"\b([A-Z]{2,5}\d{3,4})\b")
LEVEL_PATTERN = re.compile(r"^[A-Z]{2,5}(\d)\d{2,3}$")

# Intensive-session recognition, beyond the plain "Semester 1"/"Semester 2"
# substrings real semesters use. USYD's five intensive sessions and the gap each
# falls in, per sydney.edu.au/students/intensive-sessions.html:
#   Intensive January, January-February   -> summer gap (before Semester 1)
#   Intensive June-July, July-August      -> winter gap (between Sem 1 and 2)
#   Intensive November-December           -> summer gap (after Semester 2)
# CUSP uses descriptive text rather than SITS codes. The one confirmed example in
# scraped data is "Int January" on CIVL5330; the rest are inferred from the
# official session names and are NOT yet confirmed against scraped strings. If a
# scrape shows a different label, these patterns are what to update.
INTENSIVE_PATTERNS = {
    "summer": [re.compile(r"int.*jan", re.I),
               re.compile(r"int.*nov", re.I),
               re.compile(r"int.*dec", re.I)],
    "winter": [re.compile(r"int.*jun", re.I),
               re.compile(r"int.*jul", re.I),
               re.compile(r"int.*aug", re.I)],
}

# Within the summer gap two administrative teaching periods share one calendar
# window and belong to DIFFERENT real semesters for load purposes. January and
# January-February carry an S1 prefix, so their load belongs to the FOLLOWING
# year's Semester 1; November-December is S2 and belongs to the preceding
# Semester 2. Winter-gap intensives are all S2.
SUMMER_TP1_PATTERNS = [re.compile(r"int.*jan", re.I)]

PEP_BLOCK_NAME = "Professional Engagement Program (PEP) Units"


def session_gap_types(sessions):
    """Which gap types a unit's sessions_offered list reaches via an intensive."""
    found = set()
    for s in sessions or []:
        for gap_type, patterns in INTENSIVE_PATTERNS.items():
            if any(p.search(s) for p in patterns):
                found.add(gap_type)
    return found


def is_pep_unit(code, data):
    """
    Membership of the degree's PEP block, detected through degree_blocks rather
    than a hardcoded code prefix, since CUSP uses the same block name across
    engineering degrees.
    """
    return PEP_BLOCK_NAME in (data["units"].get(code, {}).get("degree_blocks") or [])


class USYDRules(rules.Rules):
    institution = "USYD"
    min_cp_per_period = MIN_CP
    max_cp_per_period = MAX_CP
    elective_slot_cp = 6
    code_pattern = CODE_PATTERN

    # ---- identity --------------------------------------------------------

    def unit_level(self, code):
        """COMP3027 -> 3000, matching how USYD describes levels."""
        m = LEVEL_PATTERN.match(code)
        return int(m.group(1)) * 1000 if m else None

    def position_label(self, index):
        year = rules.year_of(index)
        kind = position_kind(index)
        if kind == "sem1":
            return f"Year {year} - Semester 1"
        if kind == "sem2":
            return f"Year {year} - Semester 2"
        if kind == "winter_gap":
            return f"Year {year} - Winter Intensive (Jun-Aug)"
        return f"Year {year} - Summer Intensive (Nov-Feb)"

    # ---- availability ----------------------------------------------------

    def offered_in(self, code, index, data):
        """
        A discontinued unit is never offered anywhere, whatever sessions_offered
        still says, and that is checked here because every placement path comes
        through this function.

        The default differs by position kind, deliberately. For a real semester,
        missing session data or a literal "Do not offer" returns True rather than
        blocking scheduling on data that can never resolve. For a gap position the
        default flips to False: intensives are rare and only a minority of units
        run in them, so a gap slot is usable only where sessions_offered
        explicitly confirms a matching intensive.
        """
        unit = data["units"].get(code, {})
        if unit.get("discontinued"):
            return False

        sessions = unit.get("sessions_offered")
        kind = position_kind(index)

        if kind in rules.GAP_KINDS:
            if not sessions:
                return False
            wanted = "winter" if kind == "winter_gap" else "summer"
            return wanted in session_gap_types(sessions)

        if not sessions:
            return True
        if all(s.strip().lower() == "do not offer" for s in sessions):
            return True
        if kind == "sem1":
            return any("Semester 1" in s for s in sessions)
        return any("Semester 2" in s for s in sessions)

    def period_owner(self, gap_index, code, data):
        """
        Which real semester's teaching period an intensive placement's load counts
        against. A 6cp intensive is not free load: a student on a full 24cp
        semester plus an intensive carries 30cp across that period, and without
        this the engine can quietly route units through gap positions and exceed
        the overload budget without ever appearing to.
        """
        kind = position_kind(gap_index)
        if kind not in rules.GAP_KINDS:
            return None
        year = gap_index // POSITIONS_PER_YEAR
        if kind == "winter_gap":
            return year * POSITIONS_PER_YEAR + POS_SEM2
        sessions = data["units"].get(base_code(code), {}).get("sessions_offered") or []
        if any(p.search(s) for s in sessions for p in SUMMER_TP1_PATTERNS):
            return (year + 1) * POSITIONS_PER_YEAR + POS_SEM1
        return year * POSITIONS_PER_YEAR + POS_SEM2

    # ---- requisites ------------------------------------------------------

    def slot_guarantees_level(self, placeholder, min_level, data):
        """
        Can an unfilled elective slot be relied on to sit at or above `min_level`?

        Two ways it can. The block name may say so outright, as "3000+" does. Or
        the blocks the slot draws from may contain nothing below that level, which
        is the common case and the one a name check misses: the Mechatronic Year 3
        slot offers "Machine Learning Elective Units" whose two options are 3000
        and 4000 level, and the published sequence only reaches AMME4111's 36
        credit point threshold if the slot is spent there.

        A block the dataset holds no members for cannot guarantee anything. Free
        Electives is published as an open requirement rather than a list, so it is
        the reason a slot offering it is not relied on by itself.

        Returning True makes the plan depend on a choice the student has not made
        yet, so `level_conditions` reports every slot counted this way and the
        level it has to meet. Counting it silently would be the dishonest half of
        this: the plan is valid only if the student is told.
        """
        name = placeholder[len(rules.ELECTIVE_PREFIX):]
        if f"{min_level // 1000}000+" in name:
            return True

        blocks = data.get("blocks") or {}
        for part in name.split("|"):
            members = (blocks.get(part.strip()) or {}).get("members") or ()
            if not members:
                continue
            levels = [self.unit_level(m) for m in members]
            if levels and all(l is not None and l >= min_level for l in levels):
                return True
        return False

    def level_conditions(self, schedule, data):
        """
        Every unfilled slot a plan is relying on to be taken at a given level.

        One entry per slot, naming the position, the level it has to meet, the
        unit whose threshold depends on it, and the blocks that can satisfy it.
        """
        out = []
        for target, codes in sorted(schedule.items()):
            for code in codes:
                code = str(code)
                if code.startswith((rules.ELECTIVE_PREFIX, rules.FAILED_PREFIX)):
                    continue
                req = data["units"].get(rules.base_code(code), {}).get(
                    "credit_point_requirement")
                if not req or req.get("max_level") or not req.get("min_level"):
                    continue
                min_level = req["min_level"]
                for index, earlier in schedule.items():
                    if index >= target:
                        continue
                    for slot in earlier:
                        slot = str(slot)
                        if not slot.startswith(rules.ELECTIVE_PREFIX):
                            continue
                        if not self.slot_guarantees_level(slot, min_level, data):
                            continue
                        out.append({
                            "position": index,
                            "position_label": self.position_label(index),
                            "required_level": min_level,
                            "needed_by": rules.base_code(code),
                            "because": f"{rules.base_code(code)} requires "
                                       f"{req['min_credit_points']} credit points "
                                       f"of {min_level}-level or higher beforehand",
                            "choose_from": slot[len(rules.ELECTIVE_PREFIX):]})
        # one entry per (slot, level), keeping the first unit that needs it
        seen, unique = set(), []
        for entry in out:
            key = (entry["position"], entry["required_level"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return unique

    def credit_requirement_satisfied(self, code, target, schedule, data):
        """
        A generic "N credit points of [prefix][level range] units [except (...)]"
        prerequisite, validated by summing matching credit scheduled strictly
        before target.

        A FAILED: attempt does not count, having earned no credit. An ELECTIVE:
        placeholder counts only for a plain at-least-this-level requirement, where
        its block name shows it will be filled at or above that level: a bounded
        range cannot be confirmed from a placeholder's name.
        """
        req = data["units"].get(code, {}).get("credit_point_requirement")
        if not req:
            return True

        min_cp = req["min_credit_points"]
        min_level, max_level = req.get("min_level"), req.get("max_level")
        prefix = req.get("prefix")
        excluded = [re.compile(p) for p in req.get("excluded_patterns", [])]

        total = 0
        for index, codes in schedule.items():
            if index >= target:
                continue
            for c in codes:
                if c.startswith(rules.FAILED_PREFIX):
                    continue
                if c.startswith(rules.ELECTIVE_PREFIX):
                    if max_level is None and min_level is not None:
                        if self.slot_guarantees_level(c, min_level, data):
                            total += self.elective_slot_cp
                    continue
                if prefix and not c.startswith(prefix):
                    continue
                level = self.unit_level(c)
                if level is None:
                    continue
                if min_level is not None and level < min_level:
                    continue
                if max_level is not None and level > max_level:
                    continue
                if any(p.fullmatch(c) for p in excluded):
                    continue
                total += data["units"].get(c, {}).get("credit_points") or 0

        return total >= min_cp

    def prereq_satisfied(self, code, target, schedule, semester_of, prereqs, data):
        """
        In priority order: prerequisite_groups as AND-of-OR, then a flat OR list,
        then a credit-point threshold. No unit in this dataset mixes more than one.
        """
        groups = data["units"].get(code, {}).get("prerequisite_groups")
        if groups:
            return all(
                any(p in semester_of and semester_of[p] < target for p in group)
                for group in groups)

        required = prereqs.get(code, set())
        if required:
            return any(p in semester_of and semester_of[p] < target for p in required)

        if data["units"].get(code, {}).get("credit_point_requirement"):
            return self.credit_requirement_satisfied(code, target, schedule, data)
        return True

    def describe_prereq(self, code, data):
        """
        The prerequisite in words, in the same precedence order
        `prereq_satisfied` uses: grouped form, then flat list, then a
        credit-point threshold. See the UTS counterpart for why anything that
        states a rule reads the same fields that enforce it.
        """
        unit = data["units"].get(code, {})
        groups = unit.get("prerequisite_groups")
        if groups:
            return " AND ".join(
                "(" + " or ".join(dict.fromkeys(g)) + ")" for g in groups)

        required = sorted(set(unit.get("prerequisite_codes") or []))
        if required:
            return "(" + " or ".join(required) + ")"

        req = unit.get("credit_point_requirement")
        if req:
            level = (f" of {req['min_level']}-level or higher"
                     if req.get("min_level") else "")
            return f"{req['min_credit_points']}cp{level} completed"
        return None

    def describe_coreq(self, code, data):
        """The corequisite in words, from the field `coreq_satisfied` reads."""
        required = sorted(set(data["units"].get(code, {})
                              .get("corequisite_codes") or []))
        return "(" + " or ".join(required) + ")" if required else None

    def coreq_satisfied(self, code, target, semester_of, coreqs, data):
        """OR semantics, satisfied concurrently or earlier."""
        required = coreqs.get(code, set())
        if not required:
            return True
        return any(c in semester_of and semester_of[c] <= target for c in required)

    def exclusion_satisfied(self, code, semester_of, data):
        """
        Prohibitions, checked in both directions because the handbook does not
        record them symmetrically: an advanced unit's page may list the regular
        unit without the regular unit's page listing it back.
        """
        own = set(data["units"].get(code, {}).get("prohibition_codes") or [])
        for other in semester_of:
            if other == code:
                continue
            if other in own:
                return False
            if code in set(data["units"].get(other, {}).get("prohibition_codes") or []):
                return False
        return True

    def extra_constraint_satisfied(self, code, target, semester_of, data):
        """
        PEP progression: ENGP1003's page states a student must enrol in it in the
        semester immediately following ENGP1002, which an ordinary prerequisite
        cannot express.

        Uses previous_real_index rather than target - 1, because the position
        immediately before a real semester is a gap, and PEP units never run in a
        gap, so "immediately following" always means the previous real semester.
        """
        predecessor = data["units"].get(code, {}).get("immediate_successor_of")
        if not predecessor:
            return True
        if predecessor not in semester_of:
            return False
        return semester_of[predecessor] == previous_real_index(target)

    # ---- credit points ---------------------------------------------------

    def unit_cp(self, code, data):
        if code.startswith(rules.ELECTIVE_PREFIX):
            return self.elective_slot_cp
        return data["units"].get(base_code(code), {}).get("credit_points") or 0

    # ---- optional strategies ---------------------------------------------

    def elective_assigner(self):
        """USYD plans carry unresolved ELECTIVE: slots that must be filled."""
        from planner_usyd_strategies import assign_specialisation_electives
        return assign_specialisation_electives

    def threshold_resolver(self):
        from planner_usyd_strategies import resolve_credit_thresholds
        return resolve_credit_thresholds
