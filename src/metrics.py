"""
Curriculum structure metrics for Path A (Section 3.8).

The measures are those of Heileman et al. (blocking factor, delay factor,
structural complexity) and Reeping et al. (inflexibility factor), computed on the
requisite graph of a single enrolment plan. They describe a degree's shape; they
are not used here to predict graduation rates.

Two things about the graph need stating, because both change the numbers and
neither is in the original definitions.

Alternatives. A prerequisite group is a disjunction: any one member satisfies it.
Heileman's model assumes a hard edge. Treating every alternative as an edge
overstates how connected the curriculum is, and treating none of them as an edge
understates it. Both bounds are therefore computed:

    strict   an edge only where exactly one alternative appears in the plan, so
             the student has no choice about taking it
    loose    an edge for every alternative that appears in the plan

The honest figure for a degree lies between them, and the gap between the two is
itself informative: a wide gap means the degree's structure is largely optional.

Scope. The graph is restricted to units the plan actually schedules. A requisite
satisfied outside the plan, or pointing at a unit from another degree, is not an
edge in this student's curriculum.
"""

from collections import defaultdict

import rules as R

# A unit offered in one session only makes a student wait a session to retry it;
# one offered in both waits nothing. Reeping's point is that Heileman's model
# assumes every course runs every term and so misses exactly this delay.
WAIT_BOTH = 1
WAIT_ONE = 2
WAIT_UNKNOWN = 2      # treated as the single-session case rather than the best one


def _in_plan(schedule):
    return {R.base_code(str(c)) for v in schedule.values() for c in v
            if not R.is_placeholder(c)}


def build_edges(schedule, data, mode="strict"):
    """
    prereq -> {dependents}, restricted to units in the plan.

    `mode` is "strict" or "loose"; see the module docstring.
    """
    placed = _in_plan(schedule)
    forward = defaultdict(set)      # prerequisite -> units that need it
    back = defaultdict(set)         # unit -> its prerequisites

    for code in placed:
        unit = data["units"].get(code, {})
        groups = []
        typed = unit.get("prerequisite_requirements")
        if typed:
            for grp in typed:
                codes = [alt["code"] for alt in grp
                         if isinstance(alt, dict)
                         and alt.get("kind") == "subject" and alt.get("code")]
                # A group with an unverifiable alternative (a sub-major, a credit
                # total) is not a hard requirement on any one subject, so it
                # contributes no strict edge.
                other = any(not (isinstance(alt, dict)
                                 and alt.get("kind") == "subject" and alt.get("code"))
                            for alt in grp)
                groups.append((codes, other))
        else:
            for grp in (unit.get("prerequisite_groups") or []):
                groups.append((list(grp), False))
            if not groups and unit.get("prerequisite_codes"):
                groups.append((list(unit["prerequisite_codes"]), False))

        for codes, other in groups:
            present = [p for p in codes if p in placed]
            if not present:
                continue
            if mode == "strict":
                if other or len(present) != 1:
                    continue
                chosen = present
            else:
                chosen = present
            for p in chosen:
                forward[p].add(code)
                back[code].add(p)

    return placed, forward, back


def blocking_factors(placed, forward):
    """How many units transitively require each unit."""
    out = {}
    for unit in placed:
        seen, stack = set(), [unit]
        while stack:
            for nxt in forward.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        out[unit] = len(seen)
    return out


def _longest_chain(placed, edges, weight=None):
    """
    Longest path ending at each unit, memoised over the DAG.

    With no weight, counts units, so an isolated unit scores 1. With a weight
    function, accumulates weight(unit) instead, which is what turns the delay
    factor into Reeping's inflexibility factor.
    """
    w = weight or (lambda _c: 1)
    memo, visiting = {}, set()

    def walk(unit):
        if unit in memo:
            return memo[unit]
        if unit in visiting:
            return 0          # a cycle in the data, not a curriculum
        visiting.add(unit)
        best = 0
        for prev in edges.get(unit, ()):
            best = max(best, walk(prev))
        visiting.discard(unit)
        memo[unit] = best + w(unit)
        return memo[unit]

    for unit in placed:
        walk(unit)
    return memo


def delay_factors(placed, forward, back, weight=None):
    """
    Units on the longest requisite path through each unit.

    Heileman defines it over the whole path, not just the part leading up to the
    unit, so it is the chain behind it plus the chain ahead of it, less the unit
    counted twice.
    """
    up = _longest_chain(placed, back, weight)        # longest path ending here
    down = _longest_chain(placed, forward, weight)   # longest path starting here
    w = weight or (lambda _c: 1)
    return {u: up.get(u, 0) + down.get(u, 0) - w(u) for u in placed}


def wait_for(code, data):
    """Teaching periods a student waits for the next offering of `code`."""
    offered = data["units"].get(code, {}).get("sessions_offered")
    if not offered:
        return WAIT_UNKNOWN
    main = [s for s in offered
            if any(k in str(s).lower()
                   for k in ("semester 1", "semester 2", "autumn", "spring"))]
    return WAIT_BOTH if len(main) >= 2 else WAIT_ONE


def curriculum_metrics(schedule, data, mode="strict"):
    """Every metric for one plan, under one reading of the requisite groups."""
    placed, forward, back = build_edges(schedule, data, mode)
    blocking = blocking_factors(placed, forward)
    delay = delay_factors(placed, forward, back)
    inflex = delay_factors(placed, forward, back,
                           weight=lambda c: wait_for(c, data))

    edge_count = sum(len(v) for v in forward.values())
    connected = sum(1 for u in placed if forward.get(u) or back.get(u))

    return {
        "mode": mode,
        "units": len(placed),
        "requisite_edges": edge_count,
        "units_in_the_graph": connected,
        "isolated_units": len(placed) - connected,
        "blocking_factor_total": sum(blocking.values()),
        "blocking_factor_max": max(blocking.values()) if blocking else 0,
        "most_blocking_unit": (max(blocking, key=blocking.get) if blocking else None),
        "delay_factor_max": max(delay.values()) if delay else 0,
        "longest_requisite_chain": (max(_longest_chain(placed, back).values())
                                    if placed else 0),
        "structural_complexity": sum(blocking.values()) + sum(delay.values()),
        "inflexibility_factor": sum(inflex.values()),
        "single_session_units": sum(1 for u in placed
                                    if wait_for(u, data) == WAIT_ONE),
    }


def both_readings(schedule, data):
    return {"strict": curriculum_metrics(schedule, data, "strict"),
            "loose": curriculum_metrics(schedule, data, "loose")}
