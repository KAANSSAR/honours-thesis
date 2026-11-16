"""Compare core.py and rules_usyd.py against Ko's ORIGINAL planner (no 14.8 fix)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "archive"))

import curriculum_planner as cp
import core, rules, rules_usyd

data = cp.load_merged_data()
Ru = rules_usyd.USYDRules()
prereqs, coreqs, dependents = cp.build_prereq_graph(data)
sched = cp.parse_default_schedule(data)
sem = cp._semester_of_map(sched)
codes = sorted(data["units"])

bad = {}
def chk(name, a, b):
    if a != b: bad[name] = bad.get(name, 0) + 1

for c in codes:
    chk("unit_level", cp._unit_level(c), Ru.unit_level(c))
    chk("exclusion", cp._prohibition_satisfied(c, sem, data), Ru.exclusion_satisfied(c, sem, data))
    for i in range(16):
        chk("offered_in", cp.unit_offered_in(c, i, data), Ru.offered_in(c, i, data))
    for t in (0,2,4,6,8,10,12,14):
        chk("prereq", cp._prereq_satisfied(c,t,sched,sem,prereqs,data),
            Ru.prereq_satisfied(c,t,sched,sem,prereqs,data))
        chk("succession", cp._immediate_successor_satisfied(c,t,sem,data),
            Ru.extra_constraint_satisfied(c,t,sem,data))
        chk("coreq", cp._coreq_satisfied(c,t,sem,coreqs), Ru.coreq_satisfied(c,t,sem,coreqs,data))
for i in range(40):
    chk("position_kind", cp._position_kind(i), rules.position_kind(i))
    chk("position_label", cp._position_label(i), Ru.position_label(i))

print("PREDICATES vs your original planner:")
print("   mismatches:", bad or "none, every answer identical")

# load accounting: expected to differ, that IS the 14.8 fix
diff = [(i, cp.semester_credit_points(sched,i,data), core.period_credit_points(Ru,sched,i,data))
        for i in range(16) if rules.is_real(i)
        and cp.semester_credit_points(sched,i,data) != core.period_credit_points(Ru,sched,i,data)]
print(f"\nLOAD ACCOUNTING: {len(diff)} position(s) differ on this plan"
      f" (expected: core charges intensives to their teaching period)")
for i,a,b in diff: print(f"   {Ru.position_label(i)}: original {a}cp vs core {b}cp")

# replanning
placed=[c for v in sched.values() for c in v if not rules.is_placeholder(c)]
same=diffr=err=0
for c in placed:
    try: a = cp.replan_after_failure(sched, c, data, prereqs, dependents, coreqs)
    except Exception: err += 1; continue
    b = core.replan_after_failure(Ru, sched, c, data, prereqs, dependents, coreqs)
    na={k:sorted(v) for k,v in a.items() if v}; nb={k:sorted(v) for k,v in b.items() if v}
    if na==nb: same+=1
    else: diffr+=1
print(f"\nREPLANNING on {len(placed)} units: {same} identical, {diffr} differ, "
      f"{err} the original could not do")
# of the ones that differ, is core's result valid where the original's was not?
better=worse=0
for c in placed:
    try: a = cp.replan_after_failure(sched, c, data, prereqs, dependents, coreqs)
    except Exception: continue
    b = core.replan_after_failure(Ru, sched, c, data, prereqs, dependents, coreqs)
    va = cp.validate_schedule(a, prereqs, coreqs, data)
    vb = core.validate_schedule(Ru, b, prereqs, coreqs, data)
    if va and not vb: better += 1
    elif vb and not va: worse += 1
print(f"   of those, core valid where original was not: {better};  reverse: {worse}")
