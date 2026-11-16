"""
The interface the solver asks questions through, plus the position arithmetic both
institutions share.

The solver in core.py never knows which university it is working on. It asks
questions; a Rules object answers them. That is what keeps Path A honest: when USYD
and UTS produce different schedules, the difference comes from the rules, not from
two different search implementations, and that can be stated in the write-up rather
than hoped for.

What lives here is only what is genuinely common. Positions are the clearest case:
both institutions run two teaching periods a year with intensive or short sessions
in between, so both use four positions per year in calendar order. USYD fills the
gaps with its June-July and January intensives, UTS with December and July
sessions, but the arithmetic of "is this position before that one" is identical and
every prerequisite check depends on it.

What does not live here: credit-point caps, how a unit code is spelled, how a level
is read off a code, how sessions are named, how requisites are structured, and
whether specialisation electives need assigning at all. Those differ, sometimes in
ways that are easy to miss. USYD codes are letters then digits (INFO1110) and carry
their level in the first digit; UTS codes are five bare digits (48024) and carry no
level at all. A single regex cannot serve both, and the original planner's
`[A-Z]{2,5}\\d{3,4}` could not match a UTS subject at all.

A rules module may legitimately not implement an optional strategy. UTS needs no
elective assignment, because its default plans arrive with their choice groups
already resolved and their elective slots deliberately open, and it needs no
credit-threshold repair. Those return None rather than a stub that pretends to work,
and core.py skips the step and says so.
"""
import re

# Positions per year, in calendar order: first teaching period, the gap between it
# and the second, the second teaching period, then the gap before the next year.
# Four rather than two so intensive and short sessions get real, ordered positions
# in the same linear index every "target < X" prerequisite check already uses,
# instead of a parallel structure bolted alongside.
POSITIONS_PER_YEAR = 4
POS_SEM1 = 0
POS_WINTER_GAP = 1
POS_SEM2 = 2
POS_SUMMER_GAP = 3

_KIND_BY_OFFSET = {
    POS_SEM1: "sem1",
    POS_WINTER_GAP: "winter_gap",
    POS_SEM2: "sem2",
    POS_SUMMER_GAP: "summer_gap",
}
REAL_KINDS = ("sem1", "sem2")
GAP_KINDS = ("winter_gap", "summer_gap")

# Four years of four positions. Staying inside this is the compaction priority.
TARGET_POSITIONS = 4 * POSITIONS_PER_YEAR

# Backstop so an unsatisfiable case fails loudly instead of hanging.
MAX_SEARCH_HORIZON = 20

# Compaction is a greedy heuristic re-evaluated every round, so it terminates when
# a full round changes nothing. The cap exists only so that an oscillation, which
# is unlikely but not proven impossible, ends the loop instead of hanging it.
MAX_COMPACTION_ROUNDS = 50

FAILED_PREFIX = "FAILED:"
ELECTIVE_PREFIX = "ELECTIVE:"


def position_kind(index):
    """'sem1', 'winter_gap', 'sem2' or 'summer_gap' for a position index."""
    return _KIND_BY_OFFSET[index % POSITIONS_PER_YEAR]


def year_of(index):
    return index // POSITIONS_PER_YEAR + 1


def is_real(index):
    return position_kind(index) in REAL_KINDS


def next_real_index(index):
    index += 1
    while not is_real(index):
        index += 1
    return index


def previous_real_index(index):
    index -= 1
    while index >= 0 and not is_real(index):
        index -= 1
    return index


def ordered_candidates(limit):
    """
    Every position in [0, limit), real teaching periods first and gaps afterwards,
    each group ascending. Encodes "prefer an ordinary enrolment unless it delays
    graduation", which is a preference rather than a constraint, so it belongs in
    the ordering rather than in any rule.
    """
    real = [i for i in range(limit) if is_real(i)]
    gaps = [i for i in range(limit) if not is_real(i)]
    return real + gaps


def base_code(code):
    """Strip the FAILED: marker to get the real code."""
    return code[len(FAILED_PREFIX):] if code.startswith(FAILED_PREFIX) else code


def is_placeholder(code):
    return code.startswith(ELECTIVE_PREFIX) or code.startswith(FAILED_PREFIX)


def semester_of_map(schedule):
    """
    Code to the position it sits in, for placements that represent completed study.
    A FAILED: attempt earned no credit and an ELECTIVE: slot names no unit, so
    neither can satisfy anything and both are excluded.
    """
    out = {}
    for index, codes in schedule.items():
        for code in codes:
            if not is_placeholder(code):
                out[code] = index
    return out


class Rules:
    """
    What the solver may ask. Every method here has an institution-specific answer;
    anything with a shared answer is a module-level function above instead.

    Subclasses must set:
        institution, min_cp_per_period, max_cp_per_period, elective_slot_cp,
        code_pattern

    and implement the questions below. The optional strategies at the end may
    return None, meaning the institution does not need that step.
    """

    institution = None
    min_cp_per_period = None
    max_cp_per_period = None
    elective_slot_cp = 6
    code_pattern = None

    # ---- identity and shape of a unit code -------------------------------

    def find_codes(self, text):
        """Every unit code appearing in a string, in order."""
        return self.code_pattern.findall(text or "")

    def unit_level(self, code):
        """
        The code's numeric level for threshold checks (INFO1110 -> 1000), or None
        where the institution does not encode a level in the code at all, which is
        the case at UTS.
        """
        raise NotImplementedError

    def position_label(self, index):
        """Human-readable label for a position, in that institution's own terms."""
        raise NotImplementedError

    # ---- availability ----------------------------------------------------

    def offered_in(self, code, index, data):
        """Can this unit be taken in this position?"""
        raise NotImplementedError

    def period_owner(self, gap_index, code, data):
        """
        Which real teaching period a gap placement's credit load counts against,
        or None if it counts against none. A short session sits between two
        teaching periods and its load has to be charged to one of them.
        """
        raise NotImplementedError

    # ---- requisites ------------------------------------------------------

    def prereq_satisfied(self, code, target, schedule, semester_of, prereqs, data):
        raise NotImplementedError

    def coreq_satisfied(self, code, target, semester_of, coreqs, data):
        raise NotImplementedError

    def exclusion_satisfied(self, code, semester_of, data):
        """
        Mutual exclusion: USYD prohibitions, UTS anti-requisites. Not time-based,
        so it is checked against everything scheduled rather than only what comes
        earlier.
        """
        raise NotImplementedError

    def extra_constraint_satisfied(self, code, target, semester_of, data):
        """
        Any further institution-specific placement rule. USYD's PEP progression
        must fall in the immediately following teaching period, which an ordinary
        prerequisite cannot express. Default: nothing extra.
        """
        return True

    def requirements_satisfied(self, code, target, schedule, semester_of, prereqs, data):
        """Single pass/fail for the search. Validation checks the parts separately
        so it can report which one failed."""
        return (
            self.prereq_satisfied(code, target, schedule, semester_of, prereqs, data)
            and self.extra_constraint_satisfied(code, target, semester_of, data)
        )

    # ---- credit points ---------------------------------------------------

    def unit_cp(self, code, data):
        """
        Credit points for a placement, placeholders included. Must not treat a
        missing value as zero where the institution has genuine zero-credit
        subjects, which UTS does.
        """
        raise NotImplementedError

    # ---- optional strategies, may return None ----------------------------

    def elective_assigner(self):
        """Callable that fills elective slots, or None if not needed."""
        return None

    def threshold_resolver(self):
        """Callable that repairs unmet credit thresholds, or None if not needed."""
        return None
