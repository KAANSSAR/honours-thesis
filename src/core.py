"""
The solver. Institution-agnostic: it asks a Rules object questions and never knows
which university it is working on.

Every function here takes `rules` and `data` and reaches institution-specific facts
only through `rules`. Nothing in this file mentions a credit-point cap, a session
name, a code format or a requisite structure, because those all differ and the
whole point is that the search does not.

Why that matters beyond tidiness: Path A compares USYD against UTS. If the two ran
through different search implementations, any difference in the result could be the
implementation rather than the institution, and the comparison would not support
the conclusion drawn from it. One solver, two rule sets, is what makes the
comparison mean what it says.

Ported from curriculum_planner.py with behaviour held fixed. The USYD rules object
has been checked to answer identically to the original predicates on all 181 units
across every position and target, so a change in USYD output from here on is
attributable to a later change rather than to this move.

Compaction and rebalancing are deliberately still in curriculum_planner.py. They
are tuned to USYD's overload budget and are not needed for validation or
replanning, which is what the results depend on, so moving them is work with no
thesis benefit and real regression risk.
"""
from collections import defaultdict, deque

import rules as R


def build_requisite_graph(data):
    """
    prereqs[code]    -> codes, any one of which satisfies the prerequisite
    coreqs[code]     -> codes, any one of which satisfies the corequisite
    dependents[code] -> codes listing `code` as a prerequisite OR corequisite
                        alternative, the reverse edge used for replanning cascades

    Corequisite edges belong in `dependents` too: if a corequisite is displaced by
    a failure, whatever depended on it needs rechecking just as much as if it had
    been an ordinary prerequisite.
    """
    prereqs, coreqs, dependents = defaultdict(set), defaultdict(set), defaultdict(set)
    for code, unit in data["units"].items():
        for p in (unit.get("prerequisite_codes") or []):
            prereqs[code].add(p)
            dependents[p].add(code)
        for c in (unit.get("corequisite_codes") or []):
            coreqs[code].add(c)
            dependents[c].add(code)
    return prereqs, coreqs, dependents


# ---------------------------------------------------------------- load accounting

def position_credit_points(rules, schedule, index, data):
    """Credit points sitting in one position. Placeholders count at their slot size."""
    return sum(rules.unit_cp(code, data) for code in schedule.get(index, []))


def load_index(rules, target, code, data):
    """The real teaching period whose cap a placement at `target` counts against."""
    if R.is_real(target):
        return target
    return rules.period_owner(target, code, data) or target


def period_credit_points(rules, schedule, index, data):
    """
    Credit points for the whole teaching period a real position belongs to: the
    position itself plus every short-session placement charged to it.

    This is the figure every load check must use. A 6cp intensive is not free
    load, and counting only the position lets the search route units through gap
    slots and exceed the overload budget without ever appearing to.
    """
    total = position_credit_points(rules, schedule, index, data)
    for gap_index, codes in schedule.items():
        if R.is_real(gap_index):
            continue
        for code in codes:
            if rules.period_owner(gap_index, code, data) == index:
                total += rules.unit_cp(code, data)
    return total


# ---------------------------------------------------------------- validation

def validate_schedule(rules, schedule, prereqs, coreqs, data):
    """
    Check every placed unit against where it sits. Returns violation strings;
    empty means the schedule is consistent with the requirement data.

    The parts are checked separately rather than through
    rules.requirements_satisfied, so a failure can say which rule broke.
    """
    semester_of = R.semester_of_map(schedule)
    violations = []

    for code, index in semester_of.items():
        where = f"{code} ({rules.position_label(index)})"

        if not rules.prereq_satisfied(code, index, schedule, semester_of, prereqs, data):
            violations.append(f"{where} {_explain_prereq(rules, code, index, semester_of, prereqs, data)}")

        if not rules.extra_constraint_satisfied(code, index, semester_of, data):
            predecessor = data["units"].get(code, {}).get("immediate_successor_of")
            violations.append(
                f"{where} must be taken in the period immediately following "
                f"{predecessor}, and currently is not.")

        if not rules.coreq_satisfied(code, index, semester_of, coreqs, data):
            alts = ", ".join(sorted(coreqs.get(code, set()))) or "its corequisites"
            violations.append(
                f"{where} needs one of {alts} in the same period or earlier, "
                f"and none is.")

        if not rules.exclusion_satisfied(code, semester_of, data):
            violations.append(
                f"{where} is mutually exclusive with "
                f"{', '.join(_conflicts(rules, code, semester_of, data))}, "
                f"and both cannot count toward the degree.")

        # Availability. Previously missing, although Section 3.5 lists it as the
        # fifth check and the listing there shows it being reported. The search
        # enforces it when placing a unit, so plans built the ordinary way satisfy
        # it, but a schedule reaching this function by any other route was never
        # asked. That is what let a repair put a unit in a session the unit does
        # not run in and still be reported as clean.
        if not rules.offered_in(code, index, data):
            offered = data["units"].get(R.base_code(code), {}).get("sessions_offered")
            violations.append(
                f"{where} is not offered in that session"
                + (f"; it runs in {', '.join(offered)}." if offered else "."))

    # Load. Also previously missing, and for the same reason: the cap is applied
    # when placing a unit, never re-checked on a finished schedule. Checked per
    # teaching period rather than per position, so an intensive counts against the
    # period that owns it, which is how a gap placement can otherwise carry load
    # that no period appears to be charged for.
    charged = {}
    for index, codes in schedule.items():
        for code in codes:
            owner = (index if str(code).startswith(R.ELECTIVE_PREFIX)
                     else load_index(rules, index, R.base_code(str(code)), data))
            charged.setdefault(owner, 0)
    for owner in sorted(charged):
        total = period_credit_points(rules, schedule, owner, data)
        if total > rules.max_cp_per_period:
            violations.append(
                f"{rules.position_label(owner)} carries {total} credit points, "
                f"above the {rules.max_cp_per_period} credit point maximum.")

    return violations


def _explain_prereq(rules, code, index, semester_of, prereqs, data):
    """A readable reason, using whichever requisite structure this unit actually has."""
    unit = data["units"].get(code, {})

    # Typed requirements, where an alternative may be a subject or a threshold.
    for req in (unit.get("prerequisite_requirements") or []):
        if any(a.get("kind") == "subject" and a.get("code") in semester_of
               and semester_of[a["code"]] < index for a in req):
            continue
        # The source lists one alternative per phrasing, so the same alternative
        # recurs and an unmet requirement reads as "needs 96cp or 96cp or 96cp
        # or 96cp or 96cp or 96cp beforehand". Satisfaction is unaffected, being
        # an `any` over the group, but a reason a person reads should say the
        # condition once.
        parts, seen = [], set()
        for a in req:
            rendered = (a.get("code") if a.get("kind") == "subject"
                        else f"{a.get('min_credit_points')}cp"
                        if a.get("kind") == "credit_points"
                        else "a course condition")
            if rendered not in seen:
                seen.add(rendered)
                parts.append(rendered)
        return f"needs {' or '.join(parts)} beforehand, and has none of them."

    groups = unit.get("prerequisite_groups")
    if groups:
        unmet = [g for g in groups
                 if not any(p in semester_of and semester_of[p] < index for p in g)]
        return ("is missing one unit from each of these prerequisite groups: "
                + "; ".join(" or ".join(g) for g in unmet))

    credit_req = unit.get("credit_point_requirement")
    if credit_req:
        return (f"needs {credit_req['min_credit_points']}cp of matching units "
                f"scheduled earlier, and not enough are.")

    alts = ", ".join(sorted(prereqs.get(code, set()))) or "its prerequisites"
    return f"needs one of {alts} scheduled earlier, and none is."


def _conflicts(rules, code, semester_of, data):
    out = []
    for other in sorted(semester_of):
        if other == code:
            continue
        probe = {other: semester_of[other]}
        if not rules.exclusion_satisfied(code, probe, data):
            out.append(other)
    return out or ["another scheduled unit"]


# ---------------------------------------------------------------- replanning

def can_place(rules, code, target, schedule, prereqs, coreqs, data, cap=None):
    """
    Could `code` sit at `target`? Checks requisites, exclusions, availability, the
    credit cap, and that the same unit is not already in that position, which
    catches moving a successful retake into the very period it was failed in.

    `cap` defaults to the institution's maximum. Callers doing budgeted overload
    pass a tighter one.
    """
    if cap is None:
        cap = rules.max_cp_per_period
    semester_of = R.semester_of_map(schedule)
    base = R.base_code(code)

    if any(R.base_code(c) == base for c in schedule.get(target, [])):
        return False

    # A retake cannot sit at or before the attempt that failed. The search in
    # `_place` starts after the failed position and so respects this implicitly,
    # but compaction and rebalancing move units earlier and would otherwise be free
    # to place a retake before the student ever sat the unit. The failure marker
    # stays in the schedule, so the floor is read from it directly.
    failed_at = [index for index, codes in schedule.items()
                 if any(c.startswith(R.FAILED_PREFIX) and R.base_code(c) == base
                        for c in codes)]
    if failed_at and target <= max(failed_at):
        return False
    if (period_credit_points(rules, schedule, load_index(rules, target, code, data), data)
            + rules.unit_cp(code, data) > cap):
        return False
    if not rules.prereq_satisfied(code, target, schedule, semester_of, prereqs, data):
        return False
    if not rules.extra_constraint_satisfied(code, target, semester_of, data):
        return False
    if not rules.coreq_satisfied(code, target, semester_of, coreqs, data):
        return False
    if not rules.exclusion_satisfied(code, semester_of, data):
        return False
    if not rules.offered_in(code, target, data):
        return False
    return True


def _candidate_positions(rules, code, earliest, data, limit=None):
    """
    Positions to try for `code`, in preference order: every real teaching period
    from `earliest` upward, then the short-session positions.

    Real periods come first because an ordinary enrolment is preferred to an
    intensive. The gap positions have to be there at all, though: CIVL5330 runs
    only in Intensive January, so a search that steps exclusively through real
    semesters can never place it, exhausts the horizon and raises. That failure
    affected every plan carrying an intensive unit.
    """
    horizon = earliest + R.MAX_SEARCH_HORIZON * R.POSITIONS_PER_YEAR
    if limit is not None:
        horizon = min(horizon, limit + 1)
    real = [i for i in range(earliest, horizon) if R.is_real(i)]
    gaps = [i for i in range(earliest, horizon) if not R.is_real(i)]
    return real + gaps


def _place(rules, code, earliest, schedule, semester_of, prereqs, coreqs, data,
           relax=False, cap=None, limit=None):
    """
    First position at or after `earliest` where `code` fits, or None.

    The cap defaults to the institution's MINIMUM, not its maximum: this search
    never overloads a period on its own initiative. Deliberate overloading belongs
    to compaction, which decides which period has earned the room; overloading here
    would spend that headroom on whichever unit happened to be placed first.
    """
    if cap is None:
        cap = rules.min_cp_per_period
    for target in _candidate_positions(rules, code, earliest, data, limit=limit):
        ok = (rules.prereq_satisfied(code, target, schedule, semester_of, prereqs, data)
              and (relax or rules.extra_constraint_satisfied(code, target,
                                                             semester_of, data))
              and rules.coreq_satisfied(code, target, semester_of, coreqs, data)
              and rules.exclusion_satisfied(code, semester_of, data)
              and rules.offered_in(code, target, data))
        if not ok:
            continue
        room = (period_credit_points(rules, schedule,
                                     load_index(rules, target, code, data), data)
                + rules.unit_cp(code, data) <= cap)
        if room:
            return target
    return None


def replan_after_failure(rules, schedule, failed_code, data, prereqs, dependents, coreqs):
    """
    The student failed `failed_code`. Mark its slot FAILED:<code>, which still
    counts toward that period's load but satisfies nothing, then push every unit
    that depends on it, directly or transitively, forward until it is satisfied,
    offered and has room. The failed unit itself restarts from the period after
    the one it was failed in. Returns a new schedule; the input is not mutated.

    Raises RuntimeError if a unit cannot be placed within the search horizon. That
    should not happen, and exists so an unforeseen unsatisfiable case fails loudly
    instead of hanging.
    """
    new_schedule = {index: list(codes) for index, codes in schedule.items()}
    semester_of = R.semester_of_map(new_schedule)

    # The last teaching period the plan actually uses. Finishing inside it is
    # worth more to a student than avoiding one overloaded period, so the retake
    # is offered everything inside this bound before anything beyond it.
    real_used = [i for i in new_schedule if R.is_real(i) and new_schedule[i]]
    span_end = max(real_used) if real_used else None

    if failed_code not in semester_of:
        raise ValueError(f"{failed_code} is not in the current schedule, so there is "
                         f"nothing to replan.")

    # Units these same checks already reject in the plan as it arrives, judged
    # before the failure is marked so that nothing depending on the failed unit is
    # mistaken for an inherited fault.
    #
    # The UTS sequences contain placements outside the availability the subject
    # pages publish, reported as accepted findings rather than corrected (Section
    # 4.2.3). The repair loop cannot tell those from damage the failure caused, so
    # without this it drags them across the plan and charges the student for a
    # discrepancy that was in the published sequence all along. Note that
    # `validate_schedule` passes these same plans, so the two paths disagree about
    # what "valid" means; the plan the student started from is the reference here.
    # Restricted to availability, which is the one check `validate_schedule` does
    # not share. An inherited requisite fault is left to the repair loop as before,
    # because exempting it would leave the violation in every repaired plan and a
    # repair that still reports a violation is not a repair.
    _before = R.semester_of_map(schedule)
    pre_broken = {code for code, index in _before.items()
                  if not rules.offered_in(code, index, data)}

    original = semester_of[failed_code]
    slot = new_schedule[original].index(failed_code)
    new_schedule[original][slot] = f"{R.FAILED_PREFIX}{failed_code}"
    del semester_of[failed_code]

    # Breadth-first over the reverse edges, so dependents are handled in
    # dependency order rather than whatever order the dict happens to give.
    queue, affected, seen = deque([failed_code]), [], {failed_code}
    while queue:
        current = queue.popleft()
        for dep in sorted(dependents.get(current, set())):
            if dep in semester_of and dep not in seen:
                seen.add(dep)
                affected.append(dep)
                queue.append(dep)

    def move(code, earliest, is_retake=False):
        # A unit with a strict immediate-succession rule can never be validly
        # retaken under it: the one slot immediately following its predecessor is
        # the one it just failed in, and the search only looks forward. Relaxing
        # that for this unit's own retake is the most that is achievable, and its
        # ordinary prerequisite still requires the predecessor earlier. Cascaded
        # dependents still enforce the strict rule, so a chain shifts forward by
        # the gap the retake introduced rather than staying pinned to a slot that
        # is now unreachable too.
        relax = bool(is_retake
                     and data["units"].get(code, {}).get("immediate_successor_of"))
        target = None
        if is_retake and span_end is not None:
            # Inside the plan's own span first. `_place` never actually runs out
            # of room, because beyond the last published period every position is
            # empty, so without this bound the retake silently leaves the plan
            # rather than using the overload a failure has already earned.
            for tier in (None, rules.max_cp_per_period):
                target = _place(rules, code, earliest, new_schedule, semester_of,
                                prereqs, coreqs, data, relax=relax,
                                cap=tier, limit=span_end)
                if target is not None:
                    break

        if target is None:
            target = _place(rules, code, earliest, new_schedule, semester_of,
                            prereqs, coreqs, data, relax=relax)

        if target is None and is_retake:
            # Nothing takes the retake at the ordinary load. A failure has already
            # earned one overloaded period, and the retake has the strongest claim
            # on it: putting it in the first period that can hold it is the whole
            # repair, where leaving it to compaction means the cascade runs first
            # and a far messier schedule has to be repaired afterwards.
            #
            # This is tried only after the non-overloading search has failed, so
            # the allowance is never spent where a free position existed. Once the
            # retake takes it, `_cap_for` counts that period as overloaded and
            # compaction cannot grow a second one, so the budget still holds.
            target = _place(rules, code, earliest, new_schedule, semester_of,
                            prereqs, coreqs, data, relax=relax,
                            cap=rules.max_cp_per_period)

        if target is None:
            raise RuntimeError(
                f"No valid period for {code} at or after "
                f"{rules.position_label(earliest)}. Check {code}'s requisites and "
                f"availability in the {rules.institution} dataset.")
        if code in semester_of:
            new_schedule[semester_of[code]].remove(code)
        new_schedule.setdefault(target, []).append(code)
        semester_of[code] = target
        return target

    def still_satisfied(code, index):
        """Is `code` legal exactly where it sits? Same tests the repair loop uses."""
        return (rules.prereq_satisfied(code, index, new_schedule, semester_of,
                                       prereqs, data)
                and rules.coreq_satisfied(code, index, semester_of, coreqs, data)
                and rules.extra_constraint_satisfied(code, index, semester_of, data)
                and rules.offered_in(code, index, data))

    move(failed_code, R.next_real_index(original), is_retake=True)

    # Only what the failure actually broke. Being downstream of a failed unit does
    # not make a placement illegal: the retake may still land early enough to
    # satisfy it, or another alternative in its requisite group may already do so.
    #
    # Moving a dependent regardless is worse than redundant. `can_place` refuses a
    # position that already holds the same unit, so a dependent asked to move from
    # where it legally sits can only go forward, which pushes the plan out one
    # period at a time for no reason. The repair loop below still catches anything
    # a later move breaks.
    for code in affected:
        if still_satisfied(code, semester_of[code]):
            continue
        move(code, semester_of[code])

    # One pass is not enough. Dependents are placed in a fixed order, so a unit can
    # be placed while its prerequisite still sits early, and then that prerequisite
    # moves later and strands it. Failing INFO1110 did exactly this: COMP2123 is
    # processed before INFO1113, is satisfied by INFO1113 in Year 1, and is left
    # unsatisfiable once INFO1113 moves to Year 5. So repair to a fixed point:
    # re-validate, push whatever is now broken forward, and repeat until the
    # schedule stops changing.
    for _ in range(R.MAX_SEARCH_HORIZON):
        broken = []
        for code, index in sorted(R.semester_of_map(new_schedule).items(),
                                  key=lambda kv: kv[1]):
            if code in pre_broken:
                continue        # inherited from the published plan, not caused here
            if (not rules.prereq_satisfied(code, index, new_schedule, semester_of,
                                           prereqs, data)
                    or not rules.coreq_satisfied(code, index, semester_of, coreqs, data)
                    or not rules.extra_constraint_satisfied(code, index, semester_of,
                                                            data)
                    or not rules.offered_in(code, index, data)):
                broken.append(code)
        if not broken:
            break
        for code in broken:
            move(code, semester_of[code])

    # The cascade above restores validity but not compactness. `_place` never
    # overloads, so in an enrolment plan already full at the baseline every
    # displaced unit lands past the end of the plan and the degree grows by a year
    # or more. Compaction is what gives that room back, inside the overload budget.
    #
    # It is applied only if the result still validates. Compaction moves units
    # earlier, which cannot strand a dependent, but a corequisite read in the other
    # direction could in principle be disturbed, and a repair that restores
    # validity is worth more than one that is merely shorter. If the compacted
    # schedule fails to validate it is discarded and the cascade's own result is
    # returned, so this step can shorten a plan but never break one.
    compacted = compact_schedule(rules, new_schedule, data, prereqs, coreqs)
    compacted = rebalance_schedule(rules, compacted, data, prereqs, coreqs)
    if validate_schedule(rules, compacted, prereqs, coreqs, data):
        return new_schedule

    # Compaction has restored the length; this restores as much of the original
    # arrangement as the rules allow, so the student is shown the moves the
    # failure forced rather than every move the search made along the way. It is
    # accepted only if it still validates and still finishes no later, so it can
    # tidy a repair but never lengthen or break one.
    settled = settle_schedule(rules, compacted, schedule, data, prereqs, coreqs)
    if (not validate_schedule(rules, settled, prereqs, coreqs, data)
            and _span_end(settled) <= _span_end(compacted)):
        return settled
    return compacted


def _span_end(schedule):
    """The last position holding anything."""
    used = [i for i, codes in schedule.items() if codes]
    return max(used) if used else 0


def prune_empty(schedule):
    """
    Drop positions left empty. The replanning search speculatively creates a
    position while looking for room, and compaction may later drain it, leaving a
    key with no content that is not part of the real plan.
    """
    return {index: codes for index, codes in schedule.items() if codes}


def _group_codes(group):
    """
    Unit codes in a requisite group. A group is a list of codes at both
    institutions, but UTS writes some alternatives as objects carrying the code
    alongside its corequisite marker, so both shapes are accepted here rather than
    assumed anywhere downstream.
    """
    out = []
    for item in group or []:
        if isinstance(item, dict):
            code = item.get("code")
            if code:
                out.append(code)
        elif isinstance(item, str):
            out.append(item)
    return out


# ---------------------------------------------------------------- compaction

def _overloaded_count(rules, schedule, data, target):
    """Real teaching periods inside the target window already above the baseline."""
    return sum(1 for index in range(target)
               if R.is_real(index)
               and period_credit_points(rules, schedule, index, data)
               > rules.min_cp_per_period)


def _cap_for(rules, schedule, data, index, budget, target):
    """
    The credit cap this period is allowed to reach right now.

    A period already above the baseline keeps its room: it has been charged to the
    budget once and charging it again would let one busy semester consume the whole
    allowance. A period still at the baseline may grow to the maximum only while
    fewer periods are overloaded than the student has failures to absorb. One
    failure buys roughly one overloaded period, which is what keeps compaction from
    overloading the whole plan simply because the ceiling permits it.
    """
    if period_credit_points(rules, schedule, index, data) > rules.min_cp_per_period:
        return rules.max_cp_per_period
    if _overloaded_count(rules, schedule, data, target) < budget:
        return rules.max_cp_per_period
    return rules.min_cp_per_period


def compact_schedule(rules, schedule, data, prereqs, coreqs, target=None):
    """
    Pull units back inside the target window after a failure has pushed them past
    it, within an overload budget of one overloaded period per failure.

    The search in `_place` never overloads, so after a cascade every displaced unit
    sits in the first period with room at the ordinary load. In an enrolment plan
    that is already full at the baseline in every period, that is past the end of
    the plan, and the degree silently grows by a year or more. Compaction is what
    gives the room back, and it is the only step permitted to overload a period.

    Three moves run as one loop rather than in sequence, because each can unblock
    another: draining stranded units back into the window, nudging a blocking
    prerequisite earlier when the unit it blocks has nowhere legal to go, and
    topping up periods left under the baseline. Running them in a fixed order
    misses the case where a top-up repositions a prerequisite that the drain step
    had already given up on.

    Units are drained most-constrained-first, for the same reason placement is
    ordered that way: a unit with one legal period must claim it before a flexible
    unit takes it.

    Compaction only ever moves a unit earlier, so it cannot strand a dependent.
    Every move is still checked in full by `can_place`, and the caller re-validates
    the result, so a compaction that breaks anything is discarded rather than
    returned.
    """
    if target is None:
        target = R.TARGET_POSITIONS

    work = {index: list(codes) for index, codes in schedule.items()}
    budget = sum(1 for codes in work.values()
                 for c in codes if c.startswith(R.FAILED_PREFIX))
    if budget == 0:
        budget = 1

    def options(code, candidates):
        return sum(1 for t in candidates
                   if can_place(rules, code, t, work, prereqs, coreqs, data,
                                cap=_cap_for(rules, work, data, t, budget, target)))

    def try_place(code, current, candidates):
        """Prefer a position needing no overload, and spend budget only if nothing else fits."""
        for cap_rule in (lambda t: rules.min_cp_per_period,
                         lambda t: _cap_for(rules, work, data, t, budget, target)):
            for t in candidates:
                if t == current:
                    continue
                if can_place(rules, code, t, work, prereqs, coreqs, data, cap=cap_rule(t)):
                    work[current].remove(code)
                    work.setdefault(t, []).append(code)
                    return True
        return False

    def nudge_blocker_earlier(code):
        """
        `code` has nowhere legal inside the window. Whatever is currently satisfying
        each of its prerequisite groups may itself be sitting later than it needs
        to, so move the latest such unit earlier and give `code` another round.
        """
        semester_of = R.semester_of_map(work)
        unit = data["units"].get(R.base_code(code), {})
        groups = unit.get("prerequisite_groups")
        if not groups:
            required = prereqs.get(R.base_code(code)) or set()
            groups = [sorted(required)] if required else []
        blockers = []
        for group in groups:
            scheduled = [(semester_of[p], p) for p in _group_codes(group)
                         if p in semester_of]
            if scheduled:
                blockers.append(max(scheduled))
        for index, p in sorted(blockers, reverse=True):
            if try_place(p, index, R.ordered_candidates(index)):
                return True
        return False

    progress, rounds = True, 0
    while progress and rounds < R.MAX_COMPACTION_ROUNDS:
        progress = False
        rounds += 1

        last = max(work) if work else 0
        stranded = [(index, code)
                    for index in range(target, last + 1)
                    for code in list(work.get(index, []))
                    if not R.is_placeholder(code)]
        stranded.sort(key=lambda item: options(item[1], R.ordered_candidates(target)))

        leftover = []
        for index, code in stranded:
            if try_place(code, index, R.ordered_candidates(target)):
                progress = True
            else:
                leftover.append((index, code))

        if not progress:
            for index, code in leftover:
                if nudge_blocker_earlier(code):
                    progress = True
                    break

        for index in sorted(work):
            if not R.is_real(index) or index >= target:
                continue
            while (period_credit_points(rules, work, index, data)
                   < rules.min_cp_per_period):
                cap = _cap_for(rules, work, data, index, budget, target)
                # Every unit from a later period that could legally sit here,
                # rather than the first one found.
                #
                # This step used to take the first, scanning periods in order,
                # and that is how a repair came to add a year it did not need.
                # Failing INFO1110 in a Sydney software plan leaves Year 2
                # Semester 2 short, and the first units the scan meets are
                # offered in both semesters and block nothing, so they take the
                # place. SOFT2201 is offered in Semester 2 only and is the one
                # unit that has to be there, because SOFT3202 sits behind it;
                # losing that slot pushed it to Year 4 and SOFT3202 into a fifth
                # year, for a failure that needed no extension at all.
                #
                # The drain step above already orders by how few places a unit
                # can go, and the docstring claims that reason for the whole
                # function. Applying it here too is finishing that thought: a
                # unit with one legal period takes it before a unit with six.
                # Ties go to whatever is furthest out, since a unit deep in the
                # plan has the most to gain and the ones near the front can
                # usually still be fetched on a later round.
                here = []
                for later in range(index + 1, (max(work) if work else index) + 1):
                    for code in work.get(later, []):
                        if R.is_placeholder(code):
                            continue
                        if can_place(rules, code, index, work, prereqs, coreqs,
                                     data, cap=cap):
                            here.append((later, code))
                if not here:
                    break
                later, code = min(
                    here,
                    key=lambda item: (options(item[1],
                                              R.ordered_candidates(target)),
                                      -item[0]))
                work[later].remove(code)
                work.setdefault(index, []).append(code)
                progress = True

    return prune_empty(work)


def settle_schedule(rules, schedule, before, data, prereqs, coreqs, target=None):
    """
    Put back whatever did not need to move.

    The repair works by pushing every disturbed unit forward and then dragging
    what it can back again, and at no point does anything ask whether a unit
    could simply return to where the published plan had it. The result is a plan
    that finishes on time while having rearranged far more of the degree than
    the failure required: on one Sydney software plan, failing INFO1110 moved
    thirteen units where a solver proves five are enough, and the eight extra
    moves are units that were displaced on the way through and never reinstated.

    A student reads a moved unit as a change they have to absorb, so churn is
    not cosmetic. This walks the units that are no longer where they started,
    furthest-displaced first, and returns any whose original period will still
    legally take it. Every move is checked by `can_place` and the whole result
    is validated by the caller, which discards it if anything broke.

    Unlike compaction this can move a unit later, back to a position it was
    pulled in front of, so it can in principle strand a dependent. That is why
    the result is validated rather than trusted, and why the span is checked:
    a tidier plan that finishes later is not an improvement.
    """
    if target is None:
        target = R.TARGET_POSITIONS

    work = {index: list(codes) for index, codes in schedule.items()}
    budget = sum(1 for codes in work.values()
                 for c in codes if c.startswith(R.FAILED_PREFIX)) or 1

    home = {}
    for index, codes in before.items():
        for code in codes:
            if not R.is_placeholder(code) and not code.startswith(R.FAILED_PREFIX):
                home[code] = index

    for _ in range(R.MAX_COMPACTION_ROUNDS):
        now = R.semester_of_map(work)
        astray = [(code, now[code], home[code]) for code in home
                  if code in now and now[code] != home[code]]
        if not astray:
            break
        # Furthest from home first: the units dragged across several years are
        # the ones a student notices, and returning one often frees the period
        # that was holding another away from its own place.
        astray.sort(key=lambda item: -abs(item[1] - item[2]))

        moved_any = False
        for code, _was, origin in astray:
            # Read the position again rather than trusting the list: an earlier
            # move in this same pass may already have shifted this unit.
            live = R.semester_of_map(work)
            current = live.get(code)
            if current is None or current == origin:
                continue
            cap = _cap_for(rules, work, data, origin, budget, target)
            if can_place(rules, code, origin, work, prereqs, coreqs, data, cap=cap):
                work[current].remove(code)
                work.setdefault(origin, []).append(code)
                moved_any = True
                continue

            # Its own period is full, which is the usual reason a unit cannot go
            # home: something else was pulled into the slot while it was away.
            # Trading places with one of those works where moving alone does
            # not, and it is the move that unpicks most of the churn, since the
            # unit holding the slot is often one that had no reason to be there.
            # Both halves are checked against a schedule with the other half
            # lifted out, because each is only legal once the other has gone.
            for other in list(work.get(origin, [])):
                if R.is_placeholder(other) or other.startswith(R.FAILED_PREFIX):
                    continue
                trial = {i: list(c) for i, c in work.items()}
                trial[origin].remove(other)
                trial[current].remove(code)
                cap_home = _cap_for(rules, trial, data, origin, budget, target)
                cap_away = _cap_for(rules, trial, data, current, budget, target)
                if (can_place(rules, code, origin, trial, prereqs, coreqs, data,
                              cap=cap_home)
                        and can_place(rules, other, current, trial, prereqs,
                                      coreqs, data, cap=cap_away)):
                    trial.setdefault(origin, []).append(code)
                    trial.setdefault(current, []).append(other)
                    if not validate_schedule(rules, trial, prereqs, coreqs, data):
                        work = trial
                        moved_any = True
                        break
        if not moved_any:
            break

    return prune_empty(work)


def rebalance_schedule(rules, schedule, data, prereqs, coreqs, target=None):
    """
    Check every scheduled unit for a legal earlier position, not only the units
    compaction was actively placing.

    Compaction reconsiders a unit only when it is stranded or named as a blocker.
    A unit that was never either can still sit later than it needs to, holding room
    a more constrained unit wants. This asks the question for the whole plan, and
    repeats until a full pass changes nothing. An already-optimal schedule comes
    back unchanged.
    """
    if target is None:
        target = R.TARGET_POSITIONS

    work = {index: list(codes) for index, codes in schedule.items()}
    budget = sum(1 for codes in work.values()
                 for c in codes if c.startswith(R.FAILED_PREFIX))
    if budget == 0:
        budget = 1

    progress, rounds = True, 0
    while progress and rounds < R.MAX_COMPACTION_ROUNDS:
        progress = False
        rounds += 1
        for index in sorted(work):
            for code in list(work.get(index, [])):
                if R.is_placeholder(code):
                    continue
                for cap_rule in (lambda t: rules.min_cp_per_period,
                                 lambda t: _cap_for(rules, work, data, t, budget, target)):
                    placed = False
                    for t in R.ordered_candidates(index):
                        if can_place(rules, code, t, work, prereqs, coreqs, data,
                                     cap=cap_rule(t)):
                            work[index].remove(code)
                            work.setdefault(t, []).append(code)
                            progress = placed = True
                            break
                    if placed:
                        break

    return prune_empty(work)


# ---------------------------------------------------------------- reporting

def format_schedule(rules, schedule, data):
    """The schedule as lines, labelled in the institution's own terms."""
    lines = []
    for index in sorted(schedule):
        codes = schedule[index]
        if not codes:
            continue
        shown = position_credit_points(rules, schedule, index, data)
        period = (period_credit_points(rules, schedule, index, data)
                  if R.is_real(index) else None)
        load = f"{shown}cp"
        if period is not None and period != shown:
            load += f" ({period}cp across the period)"
        lines.append(f"{rules.position_label(index)}  [{load}]")
        for code in codes:
            name = data["units"].get(R.base_code(code), {}).get("name", "")
            mark = ""
            if code.startswith(R.FAILED_PREFIX):
                mark = "  (failed attempt)"
            elif code.startswith(R.ELECTIVE_PREFIX):
                mark = "  (elective slot, not yet chosen)"
            lines.append(f"    {code:28} {name[:44]}{mark}")
    return lines
