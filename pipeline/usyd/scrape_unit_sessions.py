"""
Fetch real, current sessions_offered (and discontinued status) directly
from each unit's own individual handbook page - sydney.edu.au/units/<code>
- rather than trusting CUSP's degree-structure page for this field.

Why this exists: CUSP is not a reliable source for "is this unit still
running, and in which session(s)". Confirmed two distinct failure modes
by hand on 2026-08-27, comparing CUSP's degree-structure page against
each unit's own live page:

  1. Session data can simply be STALE. SIEN2001 shows Semester 2 only on
     CUSP's degree page, but its own live page confirms both Semester 1
     2026 AND Semester 2 2026. Two official USYD sources disagreeing,
     with the individual unit page being the more granular, current one.

  2. Discontinued units aren't flagged consistently. COMP3109, ELEC3702,
     and ELEC3802 are all confirmed "no longer offered" on their own
     pages, but appear on CUSP's degree page as completely ordinary rows
     - a real session, no warning at all. Some OTHER discontinued-ish
     units (ELEC3803, ELEC5203, ELEC5701, ISYS3402) DO carry a
     "[not offered in <year>]" annotation on CUSP - but only embedded
     inside the unit's name text, which the existing scraper
     (scrape_cusp_degree.py's _parse_unit_table) has no logic to detect;
     it just folds the bracket into the name string and keeps whatever
     session sits in the next cell as if it were still valid.

Given both failure modes point the same direction - CUSP's page is not
trustworthy for this specific field, but each unit's own page is - this
script re-fetches session data at the source, for every unit code
already known from cusp_degree_structure.json (every block, so this
covers the full degree, not just the Software stream).

Output: unit_sessions.json - {code: {"sessions_offered": [...],
"discontinued": bool}}. merge_degree_data.py should treat this as the
authoritative override for sessions_offered (see its own notes), since
it's confirmed more current than CUSP's block-level listing.

Run this AFTER scrape_cusp_degree.py (needs cusp_degree_structure.json
for the full code list), and BEFORE merge_degree_data.py (which should
apply this file's data last, after everything else).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P
import os

import glob
import json
import re
import time

import requests
from bs4 import BeautifulSoup

UNIT_PAGE_URL = "https://www.sydney.edu.au/units/{}"
CUSP_STRUCTURE_FILE = P("cusp_degree_structure.json")
OUTPUT_FILE = P("unit_sessions.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Matches e.g. "Semester 1 2026", "Intensive January 2026", "Intensive
# June - July 2026" - captures everything before the first standalone
# 4-digit year, which is the actual session name. Also strips anything
# after the year (e.g. a trailing "Departmental Permission" tag, seen on
# CIVL5330's page) since that's a separate enrolment-approval flag, not
# part of the session name itself.
SESSION_CELL_PATTERN = re.compile(r"^(.*?)\s+(19|20)\d{2}\b")

DISCONTINUED_PHRASE = "no longer offered"


OTHER_DEGREES_GLOB = os.path.join(os.path.dirname(P("cusp_degree_structure.json")),
                                  "cusp_degree_[0-9]*.json")


def load_all_unit_codes():
    """
    Every unique unit code across every block in cusp_degree_structure.json
    (Software Engineering - the actual degree being planned) UNION every
    block across every cusp_degree_<id>_<name>.json file (every other
    Bachelor of Engineering stream and Advanced Computing major, if any
    exist - see scrape_other_engineering_degrees.py, which writes one file
    per degree matching the table_s_<faculty>.json convention already used
    elsewhere in this pipeline). Widening the pool this way doesn't change
    what SWE schedules against; it just means session and discontinued-
    status data ends up accurate for a much larger real-world set of
    units, not just the ones SWE's own page happens to list.
    """
    with open(CUSP_STRUCTURE_FILE) as f:
        cusp_data = json.load(f)
    codes = set()
    for block in cusp_data["unit_blocks"].values():
        for u in block["units"]:
            codes.add(u["code"])

    for path in sorted(glob.glob(OTHER_DEGREES_GLOB)):
        with open(path) as f:
            degree = json.load(f)
        for block in degree["unit_blocks"].values():
            for u in block["units"]:
                codes.add(u["code"])

    return sorted(codes)


def fetch_unit_sessions(code, timeout=10):
    """
    Fetch one unit's page and extract its CURRENT sessions_offered and
    discontinued status. Returns (result_dict, error) - result_dict is
    None if the fetch itself failed, so callers can tell "couldn't check"
    apart from "checked, and it's discontinued with no sessions".
    """
    url = UNIT_PAGE_URL.format(code)
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.HTTPError:
        return None, f"http_error_{response.status_code}"
    except requests.exceptions.RequestException as e:
        return None, f"request_failed:{e}"

    soup = BeautifulSoup(response.text, "html.parser")
    page_text = soup.get_text(separator=" ", strip=True)

    discontinued = DISCONTINUED_PHRASE in page_text.lower()

    # The "Unit availability" section renders as two separate <table>
    # elements - the first is the current year's offering(s), the second
    # is a "Previous years" history table. CONFIRMED BROKEN as originally
    # written: this used to just take tables[0], on the wrong assumption
    # that it was the first table on the page. It isn't - every unit page
    # has TWO tables before it (a "Study level / Academic unit / Credit
    # points" details table, then an "Enrolment rules" table), so
    # tables[0] was silently grabbing the details table instead, and
    # matching nothing (confirmed directly: a real run produced
    # sessions_offered=[] for every single unit uniformly, discontinued
    # flags still correct since those come from page-wide text, not a
    # table). Fixed by identifying the availability table by its actual
    # header content (a "Session" column) rather than its position -
    # robust to unrelated tables appearing anywhere earlier on the page.
    tables = soup.find_all("table")
    availability_tables = [
        t for t in tables
        if (header_row := t.find("tr")) and "session" in header_row.get_text(strip=True).lower()
    ]

    sessions = set()
    if availability_tables:
        current_year_table = availability_tables[0]
        for row in current_year_table.find_all("tr")[1:]:  # skip header row
            cells = row.find_all("td")
            if not cells:
                continue
            cell_text = cells[0].get_text(strip=True)
            match = SESSION_CELL_PATTERN.match(cell_text)
            if match:
                sessions.add(match.group(1).strip())

    return {"sessions_offered": sorted(sessions), "discontinued": discontinued}, None


def main():
    codes = load_all_unit_codes()
    print(f"Loaded {len(codes)} unique unit codes from {CUSP_STRUCTURE_FILE}.")

    results = {}
    failures = []
    discontinued_found = []
    changed_from_cusp = []

    for i, code in enumerate(codes, 1):
        print(f"[{i}/{len(codes)}] {code}...", end=" ")
        data, error = fetch_unit_sessions(code)
        if error:
            print(f"FAILED ({error})")
            failures.append((code, error))
            time.sleep(0.5)
            continue

        results[code] = data
        if data["discontinued"]:
            discontinued_found.append(code)
            print(f"DISCONTINUED (sessions on page: {data['sessions_offered']})")
        else:
            print(data["sessions_offered"])

        time.sleep(0.5)  # be polite between requests, same as other scrapers here

    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nDone. {len(results)}/{len(codes)} units fetched successfully.")
    print(f"Saved to {OUTPUT_FILE}")
    if discontinued_found:
        print(f"\n[INFO] {len(discontinued_found)} unit(s) confirmed discontinued: {discontinued_found}")
    if failures:
        print(f"\n[WARN] {len(failures)} unit(s) failed to fetch - re-run for just these, or "
              f"investigate individually: {failures}")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. FIXED 2026-08-28: originally assumed tables[0] on the page was the
#    availability table. It wasn't - a real run produced empty
#    sessions_offered for every single unit, uniformly, because tables[0]
#    is actually the "Study level / Academic unit / Credit points"
#    details table. Now identifies the right table by its header content
#    (a "Session" column) instead of position. Still worth spot-checking
#    a handful of real results after the next run (e.g. SIEN2001 should
#    come back ["Semester 1", "Semester 2"]) before trusting it fully -
#    this sandbox still can't reach sydney.edu.au to verify directly.
# 2. This will take a while - one request per unit code, 0.5s apart, for
#    every code across every block (likely 150-200+ unique codes across
#    all 20 blocks). That's the trade-off for actually-current data
#    instead of trusting CUSP's block listing.
# 3. discontinued=True units should probably be excluded from the
#    planner's candidate pools entirely, not just have empty sessions -
#    merge_degree_data.py and/or curriculum_planner.py will need a
#    decision on how to surface this (a hard filter vs. a flag left for
#    a human to review). Not decided here on purpose - that's a policy
#    question, not a scraping one.
# 4. Only checks the FIRST table for "current year" - if a unit somehow
#    has zero rows in the current-year table but real rows in the
#    "Previous years" one (shouldn't normally happen for anything still
#    running), this would report sessions_offered=[] for a unit that
#    might not actually be discontinued. The discontinued flag from the
#    page banner is the more reliable signal in that specific edge case.
