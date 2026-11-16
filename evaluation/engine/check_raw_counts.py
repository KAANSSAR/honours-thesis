"""Level 2: recompute the counts from the raw JSON, not through the engine.

Reads merged_degree_data.json, other_engineering_degrees.json and
uts_c09066_data.json directly. Nothing here calls api.Engine, core or rules, so
a figure that agrees was not produced by the code that produced the original.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import json, re, os

from paths import P

sw   = json.load(open(P("merged_degree_data.json")))
oth  = json.load(open(P("other_engineering_degrees.json")))
uts  = json.load(open(P("uts_c09066_data.json")))
picks= json.load(open(P("uts_plan_picks.json")))

def has_req(u):
    return bool(u.get("prerequisite_codes") or u.get("prerequisite_groups")
                or u.get("prerequisite_requirements"))

# ---- Software dataset ------------------------------------------------------
swu = sw["units"]
print(f"Software units                 : {len(swu)}        (thesis says 181)")
print(f"  carrying no requisite        : {sum(1 for u in swu.values() if not has_req(u))}        (thesis says 104)")

# ---- the other seven degrees, distinct codes -------------------------------
pool = {}
for degree in oth.values():
    if not isinstance(degree, dict):
        continue
    for block in (degree.get("unit_blocks") or {}).values():
        for rec in block.get("units", []):
            c = rec.get("code")
            if c:
                pool[c] = pool.get(c, False) or has_req(rec)
print(f"other seven, distinct units    : {len(pool)}        (thesis says 318)")
print(f"  carrying no requisite        : {sum(1 for v in pool.values() if not v)}        (thesis says 225)")

# ---- placement counts from the semester plans ------------------------------
def count_usyd(plan_blob):
    n = 0
    for entry in plan_blob:
        n += 1
    return n

usyd_placements = 0
for degree in oth.values():
    if isinstance(degree, dict) and degree.get("semester_plan"):
        pass
print()
print("UTS sequences                 :", len(uts.get("sequences") or {}))
fixed = sum(1 for s in (uts.get("sequences") or {}).values()
            for e in s if e.get("kind") == "fixed")
choice = sum(1 for s in (uts.get("sequences") or {}).values()
             for e in s if e.get("kind") == "choice")
print(f"  fixed placements across all  : {fixed}")
print(f"  choice entries               : {choice}")
print(f"UTS total credit points        : {uts.get('total_credit_points')}   (thesis says 198)")
print(f"UTS structure                  : {uts.get('structure')}")

print()
print(f"accepted findings declared, UTS : {len(picks.get('accepted_findings') or [])}")
if os.path.exists(P("usyd_plan_picks.json")):
    up = json.load(open(P("usyd_plan_picks.json")))
    print(f"accepted findings declared, USYD: {len(up.get('accepted_findings') or [])}")
    print(f"  total                         : "
          f"{len(picks.get('accepted_findings') or []) + len(up.get('accepted_findings') or [])}"
          f"        (thesis says 6)")
