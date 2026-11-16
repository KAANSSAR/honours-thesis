"""Apply confirmed data corrections to the scraped USYD dataset.

Every change is recorded in data_overrides.json with its reason and source, so the
corrections are auditable and can be reported in the thesis rather than applied
silently. Files are found through src/paths.py, so it runs from anywhere.
"""
import json, glob, os, sys, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from paths import P

# MATH1X61 -> MATH1061/MATH1961, MATH1X62 -> MATH1062/MATH1962.
# The scraper's wildcard expansion dropped the standard variants, leaving only
# advanced and discontinued codes. Confirmed against each unit's own page.
MATHS = {
    "MATH2061": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
    "AMME2261": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
    "AMME2262": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
    "BMET2960": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
    "STAT2011": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
    "STAT2911": [["MATH1061", "MATH1961"], ["MATH1062", "MATH1962"]],
}
MATHS_REASON = ("Wildcard prerequisite expansion dropped the standard variants. "
                "MATH1X61 expands to MATH1061 and MATH1961, MATH1X62 to MATH1062 "
                "and MATH1962. Confirmed against sydney.edu.au/units/<code>.")

SESSIONS = {
    "SIEN2001": (["Semester 1", "Semester 2"],
                 "Recorded as Semester 2 only in every degree's block data; it also "
                 "runs in Semester 1."),
    "CHNG5003": (["Semester 1", "Semester 2"],
                 "CUSP places this core unit in Year 3 Semester 1 of the Environmental "
                 "base plan, so Semester 1 availability is taken from the published plan."),
    "ENVE3002": (["Semester 1", "Semester 2"],
                 "CUSP places this core unit in Year 3 Semester 2 of the Environmental "
                 "base plan, so Semester 2 availability is taken from the published plan."),
    "CIVL3614": (["Semester 2"],
                 "Recorded as 'Do not offer' but CUSP lists it as a core unit in Year 3 "
                 "Semester 2 of the current Environmental base plan."),
}

# A different kind of fault from the wildcards above. DATA2002's published
# prerequisite is a top-level OR with an AND nested inside one bracket:
#
#   DATA1X01 or ENVX1002 or BUSS1020 or ECMT1010 or MATH1062 or MATH1962
#   or MATH1972 or [MATH1X05 and (MATH1001 or ... or MATH1115 or MATH19XX)]
#
# The scraper split on the bracket and stored the two halves as separate
# AND-groups, which turns the bracketed alternative into a requirement: a student
# holding MATH1062 alone satisfies the real rule but was rejected, because the
# second group demanded a MATH1000-level unit as well. All three Engineering Data
# Science default plans carry MATH1062 and were failing on it.
#
# "A or (B and C)" is expressible as an AND of ORs by distributing it, which is
# what the planner's prerequisite_groups actually means:
#
#   (T or B) and (T or C),  where T is the list of top-level alternatives
#
# so MATH1062, being in T, satisfies both groups on its own, while a student
# taking the MATH1X05 route still satisfies them via B and C. Writing T as a
# single flat group instead would wrongly reject that second route.
#
# Wildcards are expanded as the handbook defines them: DATA1X01 to DATA1001 and
# DATA1901, MATH1X05 to MATH1005 and MATH1905, MATH19XX to the MATH1900-level
# units. Codes absent from this dataset are kept rather than dropped, since they
# remain valid alternatives for a student who transferred them in and an absent
# code simply never matches.
_DATA2002_TOP = ["DATA1001", "DATA1901", "ENVX1002", "BUSS1020", "ECMT1010",
                 "MATH1062", "MATH1962", "MATH1972"]
_DATA2002_B = ["MATH1005", "MATH1905"]
_DATA2002_C = ["MATH1001", "MATH1002", "MATH1003", "MATH1004", "MATH1021",
               "MATH1023", "MATH1115", "MATH1901", "MATH1902", "MATH1903",
               "MATH1906", "MATH1907", "MATH1921", "MATH1923", "MATH1931",
               "MATH1933", "MATH1961", "MATH1962", "MATH1964", "MATH1971",
               "MATH1972"]
_DATA2002_GROUPS = [sorted(set(_DATA2002_TOP + _DATA2002_B)),
                    sorted(set(_DATA2002_TOP + _DATA2002_C))]
_DATA2002_REASON = (
    "The published prerequisite is a top-level OR with an AND inside one bracket: "
    "DATA1X01 or ENVX1002 or BUSS1020 or ECMT1010 or MATH1062 or MATH1962 or "
    "MATH1972 or [MATH1X05 and (MATH1001 or ... or MATH1115 or MATH19XX)]. The "
    "scraper split on the bracket and stored the halves as AND-groups, making the "
    "bracketed alternative compulsory. Rewritten as the distributed form "
    "(T or MATH1X05) and (T or the MATH1000 list), which is equivalent and which "
    "prerequisite_groups can express. Confirmed against the unit's own page.")

# DATA2902 is the advanced version and its page carries the same clause, so it has
# the same fault. It is not scheduled in any default plan, so it is corrected for
# consistency rather than because anything currently depends on it.
STRUCTURED_PREREQS = {
    "DATA2002": (_DATA2002_GROUPS, _DATA2002_REASON),
    "DATA2902": (_DATA2002_GROUPS, _DATA2002_REASON
                 + " DATA2902 is the advanced version and carries the same clause."),
}

log = []


def note(path, code, field, before, after, reason):
    log.append({"file": path, "unit": code, "field": field,
                "before": before, "after": after, "reason": reason})


def patch_unit_record(rec, code, path):
    changed = False
    if code in MATHS:
        groups = MATHS[code]
        flat = [c for g in groups for c in g]
        if rec.get("prerequisite_codes") != flat:
            note(path, code, "prerequisite_codes", rec.get("prerequisite_codes"), flat, MATHS_REASON)
            rec["prerequisite_codes"] = flat
            changed = True
        if rec.get("prerequisite_groups") != groups:
            note(path, code, "prerequisite_groups", rec.get("prerequisite_groups"), groups, MATHS_REASON)
            rec["prerequisite_groups"] = groups
            changed = True
    if code in STRUCTURED_PREREQS:
        groups, reason = STRUCTURED_PREREQS[code]
        flat = sorted({c for g in groups for c in g})
        if rec.get("prerequisite_groups") != groups:
            note(path, code, "prerequisite_groups", rec.get("prerequisite_groups"),
                 groups, reason)
            rec["prerequisite_groups"] = groups
            changed = True
        if sorted(rec.get("prerequisite_codes") or []) != flat:
            note(path, code, "prerequisite_codes", rec.get("prerequisite_codes"),
                 flat, reason)
            rec["prerequisite_codes"] = flat
            changed = True
    if code in SESSIONS:
        want, reason = SESSIONS[code]
        if rec.get("sessions_offered") != want:
            note(path, code, "sessions_offered", rec.get("sessions_offered"), want, reason)
            rec["sessions_offered"] = want
            changed = True
    return changed


table_s_dir = os.path.dirname(P("table_s_arts_units.json"))
targets = ["merged_degree_data.json", "stream_table_units.json",
           "other_engineering_degrees.json"] + sorted(
    os.path.basename(f) for f in glob.glob(os.path.join(table_s_dir, "table_s_*.json")))

for name in targets:
    path = P(name)
    if not os.path.exists(path):
        continue
    j = json.load(open(path))
    hit = False

    if name == "other_engineering_degrees.json":
        for did, v in j.items():
            for bn, b in v.get("unit_blocks", {}).items():
                for u in b.get("units", []):
                    if patch_unit_record(u, u.get("code"), f"{name}:{did}:{bn}"):
                        hit = True
    else:
        u = j.get("units")
        if isinstance(u, dict):
            for code, rec in u.items():
                if patch_unit_record(rec, code, name):
                    hit = True
        elif isinstance(u, list):
            for rec in u:
                if patch_unit_record(rec, rec.get("code"), name):
                    hit = True

    if hit:
        json.dump(j, open(path, "w"), indent=1)
        print("patched", name)

out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
       "change_count": len(log), "changes": log}
json.dump(out, open(P("data_overrides.json"), "w"), indent=1)
print(f"\n{len(log)} field changes written to data_overrides.json")
for c in log:
    print(f"  {c['unit']:9} {c['field']:22} {str(c['before'])[:48]:50} -> {str(c['after'])[:40]}")
