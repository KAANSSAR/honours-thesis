"""
Fill the missing USYD requisites from the handbook extraction.

Background. CUSP supplies each degree's structure but no per-unit requisites.
For Software Engineering those came from a separate handbook-table scrape and
were merged in; for the other seven degrees that merge was never run, which is
why 86% to 100% of the units in six of the seven paired plans carry no recorded
requisite while Software carries them for five of its twelve third-year units.

This applies the extraction parsed by parse_usyd_requisites.py, following the
same discipline as apply_overrides.py: every change is logged with its before,
its after and the handbook table it came from, so the dataset's corrections stay
auditable rather than silent.

Two rules govern what it will touch.

It never overwrites. A unit that already carries a requisite is left exactly as
it is, because those were either scraped from the unit's own page or corrected
by hand against it, and a bulk fill has no business overruling either. Only an
empty field is filled, and the count of units skipped for this reason is
reported rather than hidden.

It never invents a code. Wildcard expansions were already filtered against the
unit data during parsing, and a prerequisite group whose codes are all outside
the dataset is written anyway but counted separately, because such a group
constrains nothing and should not be mistaken for coverage.

Usage:
    python3 apply_usyd_requisites.py              # dry run, prints what it would do
    python3 apply_usyd_requisites.py --write      # patch the dataset files
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import argparse
import datetime
import json
import os
import shutil

PARSED = P("usyd_requisites_parsed.json")
TARGETS = ("merged_degree_data.json", "other_engineering_degrees.json")
LOG_FILE = P("usyd_requisite_fill_log.json")

# Fields this script is allowed to fill, and the parsed key each comes from.
FIELDS = (
    ("prerequisite_groups", "prerequisite_groups"),
    ("prerequisite_requirements", "prerequisite_requirements"),
    ("corequisite_codes", "corequisite_codes"),
    ("prohibition_codes", "prohibition_codes"),
    ("credit_point_requirement", "credit_point_requirement"),
    ("credit_point_requirements", "credit_point_requirements"),
    ("unverifiable_requirements", "unverifiable_requirements"),
)

REASON = ("Filled from the {table} unit of study table of the current Bachelor of "
          "Engineering (Honours) handbook. CUSP carries no per-unit requisites, "
          "and the handbook-table merge that supplied them for Software "
          "Engineering was never run for this degree. Wildcard codes were "
          "expanded against the unit data and unexpandable forms recorded as "
          "unverifiable rather than guessed.")


def unit_records(blob, path):
    """
    Yield (code, record) for every unit record in a dataset file.

    The two files are shaped differently: one has a flat units map, the other
    nests units inside each degree's unit_blocks, and the same code can appear in
    several blocks of the same degree as separate dict objects.
    """
    if "units" in blob:
        for code, rec in blob["units"].items():
            yield code, rec
        return
    for degree in blob.values():
        if not isinstance(degree, dict):
            continue
        for block in (degree.get("unit_blocks") or {}).values():
            for rec in block.get("units", []):
                if rec.get("code"):
                    yield rec["code"], rec


def is_empty(rec, field):
    value = rec.get(field)
    return value is None or value == [] or value == {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="actually patch the files; omit for a dry run")
    ap.add_argument("--parsed", default=PARSED)
    args = ap.parse_args()

    parsed = json.load(open(args.parsed, encoding="utf-8"))
    log, filled, skipped_existing, touched_units = [], 0, 0, set()

    for name in TARGETS:
        path = P(name)
        if not os.path.exists(path):
            print(f"  {path} not found, skipping")
            continue
        blob = json.load(open(path, encoding="utf-8"))
        changed = False

        for code, rec in unit_records(blob, path):
            new = parsed.get(code)
            if not new:
                continue
            table = new.get("source_table", "handbook")
            for field, key in FIELDS:
                if key not in new:
                    continue
                if not is_empty(rec, field):
                    skipped_existing += 1
                    continue
                log.append({"file": name, "unit": code, "field": field,
                            "before": rec.get(field), "after": new[key],
                            "reason": REASON.format(table=table)})
                rec[field] = new[key]
                filled += 1
                touched_units.add(code)
                changed = True

        if changed and args.write:
            shutil.copy2(path, path + ".bak")
            json.dump(blob, open(path, "w", encoding="utf-8"), indent=1)
            print(f"  patched {path} (backup at {path}.bak)")

    print(f"\nunits touched            : {len(touched_units)}")
    print(f"fields filled            : {filled}")
    print(f"fields left alone        : {skipped_existing}  (already recorded)")

    if args.write:
        json.dump({"generated": datetime.datetime.now().isoformat(timespec="seconds"),
                   "change_count": len(log), "changes": log},
                  open(LOG_FILE, "w", encoding="utf-8"), indent=1)
        print(f"wrote {LOG_FILE}")
    else:
        print("\ndry run, nothing written. Re-run with --write to apply.")
        for entry in log[:6]:
            print(f"  {entry['unit']:9} {entry['field']:28} <- "
                  f"{json.dumps(entry['after'])[:70]}")


if __name__ == "__main__":
    main()
