"""
Fetch CUSP degree structure for every OTHER Bachelor of Engineering
Honours stream and Bachelor of Advanced Computing major - everything
except Software Engineering (degree_id 454), which is already covered
by scrape_cusp_degree.py and stays the actual degree the planner
schedules against.

Why this exists: a Software Engineering student can encounter units from
these other degrees in two ways that already showed up as real problems
this session - the shared "Projects Table" intro units (AERO1560,
MECH1560, MTRX1701, etc. - each stream's own intro unit, all listed on
SWE's own CUSP page) and cross-faculty breadth specialisation units. But
the WIDER motivation is simpler: the same CUSP staleness and
discontinued-unit problems confirmed for Software Engineering
(SIEN2001's session data, COMP3109/ELEC3702/ELEC3802 discontinued with
no warning) are just as likely to exist in every other engineering
degree's own CUSP page, and there's no reason to assume otherwise until
checked.

FILE LAYOUT - one file per degree (2026-08-28 fix): originally this
wrote one combined other_engineering_degrees.json. Wrong call - this
codebase already has an established pattern for exactly this situation
(scrape_all_table_s.py writes one table_s_<faculty>_units.json per
faculty, not one merged file), and a combined file made the real content
duplication across degrees (shared breadth blocks, shared BAC major/minor
options - genuinely present on every relevant CUSP page, not a scraping
bug - see note 3 at the bottom) harder to review per-degree. Now writes
cusp_degree_<id>_<slug>.json per degree, matching that convention.

This does NOT replace or touch cusp_degree_structure.json (Software
Engineering's own file, which curriculum_planner.py schedules against
via its semester_plan/specialisations - that stays SWE-specific, that's
the actual thing being planned). These per-degree files exist purely to
widen the unit-code pool that scrape_unit_sessions.py checks for accurate
session/discontinued data - see load_all_unit_codes() there, which globs
every cusp_degree_*.json file and unions the codes.

Reuses fetch_print_view()/parse_unit_blocks()/parse_semester_plan()/
parse_specialisations() from scrape_cusp_degree.py directly rather than
duplicating that parsing logic - same page format, same DEGREE_ID
parameter already built in, just looped over every ID here instead of
one hardcoded constant.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P
import os

import json
import re
import time

from scrape_cusp_degree import (
    fetch_print_view,
    parse_unit_blocks,
    parse_semester_plan,
    parse_specialisations,
    PRINT_VIEW_URL,
)

# From the user-provided CUSP degree list, 2026-08-28. Excludes
# Software Engineering (454) - already covered separately.
DEGREE_IDS = {
    442: "Aeronautical Engineering",
    445: "Aeronautical Engineering with Space",
    456: "Biomedical Engineering",
    455: "Chemical & Biomolecular Engineering",
    448: "Civil Engineering",
    449: "Electrical Engineering",
    1235: "Environmental Engineering",
    443: "Mechanical Engineering",
    446: "Mechanical Engineering with Space",
    444: "Mechatronic Engineering",
    447: "Mechatronic Engineering with Space",
    754: "Bachelor of Advanced Computing (Computational Data Science)",
    751: "Bachelor of Advanced Computing (Computer Science)",
    1154: "Bachelor of Advanced Computing (Cybersecurity)",
    753: "Bachelor of Advanced Computing (Software Development)",
}


def _slug(name):
    """'Aeronautical Engineering with Space' -> 'aeronautical_engineering_with_space'"""
    s = re.sub(r"[^\w\s-]", "", name.lower())
    return re.sub(r"[\s-]+", "_", s).strip("_")


def output_filename(degree_id, name):
    return os.path.join(os.path.dirname(P("cusp_degree_structure.json")),
                        f"cusp_degree_{degree_id}_{_slug(name)}.json")


def main():
    written = []
    failures = []

    for i, (degree_id, name) in enumerate(DEGREE_IDS.items(), 1):
        print(f"[{i}/{len(DEGREE_IDS)}] {name} (degree_id={degree_id})...")
        soup = fetch_print_view(degree_id=degree_id)
        if soup is None:
            print(f"  FAILED to fetch - skipping")
            failures.append((degree_id, name))
            time.sleep(1)
            continue

        unit_blocks = parse_unit_blocks(soup)
        semester_plan = parse_semester_plan(soup)
        specialisations = parse_specialisations(soup, unit_blocks)

        total_units = sum(len(b["units"]) for b in unit_blocks.values())
        degree_codes = set()
        for block_name, block in unit_blocks.items():
            codes = [u["code"] for u in block["units"]]
            degree_codes.update(codes)
            print(f"    - {block_name} ({len(codes)}): {codes}")
        print(f"  {len(unit_blocks)} unit blocks, {total_units} total unit entries, "
              f"{len(degree_codes)} unique codes, {len(specialisations)} specialisations")
        if total_units == 0:
            print(f"  [WARN] Got zero units - same heading/table-column risk noted in "
                  f"scrape_cusp_degree.py's own docstring; this degree's page may have "
                  f"a different structure worth checking by hand.")

        outfile = output_filename(degree_id, name)
        with open(outfile, "w") as f:
            json.dump({
                "degree_id": degree_id,
                "name": name,
                "source_url": PRINT_VIEW_URL.format(degree_id),
                "unit_blocks": unit_blocks,
                "semester_plan": semester_plan,
                "specialisations": specialisations,
            }, f, indent=2)
        written.append(outfile)
        print(f"  Saved to {outfile}")

        time.sleep(1)  # same politeness pattern as the other scrapers here

    print(f"\n{'='*70}\nDone. {len(written)}/{len(DEGREE_IDS)} degrees fetched successfully.")
    for f in written:
        print(f"  - {f}")
    if failures:
        print(f"\n[WARN] {len(failures)} degree(s) failed to fetch - re-run for just these: {failures}")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. NOT YET RUN by me - built from the real, working scrape_cusp_degree.py
#    functions (confirmed working for Software Engineering), but each
#    OTHER degree's page could have a different section structure CUSP
#    hasn't been checked against yet. The [WARN] on zero units per-degree
#    is there specifically to catch that per-degree rather than only at
#    the end.
# 2. This will take a while - 15 degree fetches, 1s apart between each.
#    Much lighter than scrape_unit_sessions.py's per-unit fetching (that's
#    still a separate step run afterward, against the combined code pool).
# 3. Real content duplication ACROSS the output files is expected, not a
#    bug: the 4 Bachelor of Advanced Computing files will share near-
#    identical Table A Electives/Advanced Electives/Major-Minor blocks;
#    the 11 engineering-stream files will share near-identical breadth
#    specialisation blocks. That's what CUSP's own pages actually show
#    for each of them - confirmed directly from a real fetch, not assumed.