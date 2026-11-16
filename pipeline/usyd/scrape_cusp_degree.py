"""
Scraper for the University of Sydney CUSP (Course & Unit of Study Portal)
degree page — a more complete source than the raw handbook stream-table
page scraped by scale_live_fetch.py.

Why this source instead:
  - The handbook stream-table page (used by scale_live_fetch.py) only
    lists the Software Stream's own core/elective units (Blocks 4-6 in
    CUSP's numbering). It never touches Introductory Computing (Block 0),
    Foundation Maths (Block 1), the Projects Table (Block 2), PEP units
    (Block 3), or Free Electives (Block 7) — all of which are degree
    requirements, just not part of the stream table itself.
  - CUSP's degree page has ALL of these blocks in one place, AND the
    official Year 1 Sem 1 -> Year 4 Sem 2 enrolment structure — i.e. USYD's
    own answer to "what does a valid sequence through this degree look
    like", which is exactly what Path B's planner needs to validate
    replans against rather than reconstruct from prerequisite data alone.

On the "Download as PDF" button: it's wired to a client-side JS function
(downloadPDF()), which requests/BeautifulSoup can't trigger — that needs a
real browser. Instead, this scrapes the page's "Print View" link, a plain
URL that serves the same content as unstyled HTML with no JS required:
    https://cusp.sydney.edu.au/students/view-degree-page/print/1/degree_id/<id>/
It's actually easier to scrape than the live page — the print view's
section headings are plain text (not wrapped in <a> tags like the live
page's headings are).

This has been run successfully against the real page (degree_id=454,
Software Engineering) and produces correct output for unit_blocks and
semester_plan. parse_specialisations() (added later) hasn't been run
against the live page yet - tested against a synthetic snippet built
from the real heading text confirmed via a direct fetch of the print
view, but worth double-checking the printed specialisation list and any
[WARN: ... not matched] flags on the first real run.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import requests
from bs4 import BeautifulSoup
import json
import re

PRINT_VIEW_URL = "https://cusp.sydney.edu.au/students/view-degree-page/print/1/degree_id/{}/"
HEADERS = {"User-Agent": "Mozilla/5.0"}

DEGREE_ID = 454  # Software Engineering (2026)
OUTPUT_FILE = P("cusp_degree_structure.json")

BLOCK_HEADING_PATTERN = re.compile(r"^Block\s+\d+\s*-\s*(.+?)(?:\s*\(([^)]*)\))?$")
YEAR_HEADING_PATTERN = re.compile(r"^Year\s+\d+\s*-\s*.+$")

# Matches e.g. "1. Stream Specialisation in Computer Engineering" or
# "5. Breadth Specialisation in Humanitarian Engineering", under the
# "4. Pathways" section - the ONLY place CUSP states whether a
# specialisation is stream or breadth type. The unit blocks themselves
# (Section 5, "Specialisation - <Name> Core/Elective Units") never say
# which type they are - that distinction only exists here.
SPECIALISATION_HEADING_PATTERN = re.compile(
    r"^\d+\.\s+(Stream|Breadth)\s+Specialisation\s+in\s+(.+)$", re.IGNORECASE
)


def fetch_print_view(degree_id=DEGREE_ID, timeout=15):
    url = PRINT_VIEW_URL.format(degree_id)
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print("  [ERROR] Timeout fetching CUSP print view")
        return None
    except requests.exceptions.HTTPError:
        print(f"  [ERROR] HTTP error fetching CUSP print view: status {response.status_code}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] Request failed fetching CUSP print view: {e}")
        return None

    return BeautifulSoup(response.text, "html.parser")


def _heading_table_pairs(soup, heading_tags=("h2", "h3", "h4")):
    """
    Walk the document in order and pair each heading with the table that
    immediately follows it, IF one does before the next heading. This is
    the piece heading.find_next('table') got wrong: find_next() searches
    forward through the whole document regardless of section boundaries,
    so a heading with no table of its own (e.g. 'Free Electives', which is
    pure prose) would silently grab the *next* section's table instead of
    correctly having none.

    Yields (heading_tag, table_tag_or_None) in document order.
    """
    elements = soup.find_all(list(heading_tags) + ["table"])
    i = 0
    while i < len(elements):
        el = elements[i]
        if el.name in heading_tags:
            table = None
            if i + 1 < len(elements) and elements[i + 1].name == "table":
                table = elements[i + 1]
            yield el, table
        i += 1


def _parse_unit_table(table):
    units = []
    if table is None:
        return units
    rows = table.find_all("tr")
    for row in rows[1:]:  # skip header row
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        code = cells[0].get_text(strip=True)
        name = cells[1].get_text(strip=True)
        cp_text = cells[2].get_text(strip=True)
        sessions_text = cells[3].get_text(separator="|", strip=True) if len(cells) > 3 else ""
        units.append({
            "code": code,
            "name": name,
            "credit_points": int(cp_text) if cp_text.isdigit() else None,
            "sessions_offered": [s for s in sessions_text.split("|") if s],
        })
    return units


def parse_unit_blocks(soup):
    """
    Parse every 'Block N - <name> (Min CP: x,Max CP: y)' section into a
    structured list of units. Returns:
        {block_name: {"min_cp": int|None, "max_cp": int|None, "units": [...]}}

    A block with no table of its own (e.g. 'Free Electives', which is pure
    prose describing eligible units rather than a fixed list) correctly
    gets units=[] rather than borrowing the next block's table — see
    _heading_table_pairs.
    """
    blocks = {}
    for heading, table in _heading_table_pairs(soup):
        text = heading.get_text(strip=True)
        match = BLOCK_HEADING_PATTERN.match(text)
        if not match:
            continue

        block_name = match.group(1).strip()
        cp_info = match.group(2) or ""
        min_cp_match = re.search(r"Min CP:\s*(\d+)", cp_info)
        max_cp_match = re.search(r"Max CP:\s*(\d+)", cp_info)
        min_cp = int(min_cp_match.group(1)) if min_cp_match else None
        max_cp = int(max_cp_match.group(1)) if max_cp_match else None

        blocks[block_name] = {
            "min_cp": min_cp,
            "max_cp": max_cp,
            "units": _parse_unit_table(table),
        }

    return blocks


def parse_semester_plan(soup):
    """
    Parse every 'Year X - Semester Y' (and 'Year 0 - Planning your degree')
    section into the official enrolment structure: which requirement type
    and how many CP is slotted into which semester, and what it can be
    satisfied from.

    ASSUMPTION TO VERIFY: these tables are [Type, CP, CP From] — 3 columns,
    where "CP From" holds either a direct unit link/code or a "Select from
    Unit Blocks: ..." reference to one or more blocks. This function keeps
    that cell as raw text (raw_options) rather than resolving it, since
    resolving it accurately depends on the real link structure, which is
    worth checking once real HTML is in hand.
    """
    plan = []
    for heading, table in _heading_table_pairs(soup):
        text = heading.get_text(strip=True)
        if not YEAR_HEADING_PATTERN.match(text):
            continue

        if table is None:
            plan.append({"period": text, "entries": []})
            continue

        entries = []
        rows = table.find_all("tr")
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            block_type = cells[0].get_text(strip=True)
            cp_text = cells[1].get_text(strip=True)
            raw_options = cells[2].get_text(separator=" | ", strip=True)
            entries.append({
                "block_type": block_type,
                "credit_points": int(cp_text) if cp_text.isdigit() else None,
                "raw_options": raw_options,
            })

        plan.append({"period": text, "entries": entries})

    return plan


def parse_specialisations(soup, unit_blocks):
    """
    Parse the '4. Pathways' section headings into a structured list
    identifying which specialisations this degree offers, which TYPE
    each is (stream vs breadth), and which Core/Elective unit blocks
    each one maps to.

    Generic across degrees/streams: CUSP uses this same
    'N. Stream/Breadth Specialisation in <Name>' heading convention on
    every engineering degree page (spot-checked directly against
    Mechanical Engineering with Space's degree page too, which has a
    different number of specialisations and different CP splits) - this
    function doesn't hardcode any specific specialisation name or count,
    only the heading pattern itself.

    Rather than re-parsing the pathway table's own cells (which would
    duplicate parse_unit_blocks' work and risk drifting out of sync with
    it), this derives each specialisation's Core/Elective block names
    from the standard "Specialisation - <Name> Core/Elective Units"
    naming convention and verifies they actually exist in unit_blocks -
    core_block/elective_block are None if a match isn't found, so a
    caller can tell "genuinely no elective block for this specialisation"
    apart from "the naming assumption didn't hold".

    KNOWN CASE HANDLED: a specialisation's pathway-heading name can carry
    a trailing abbreviation the block names don't - e.g. the heading
    reads "Internet of Things (IoT)" but the block is just "Specialisation
    - Internet of Things Core Units" (no "(IoT)"). A trailing
    parenthetical is stripped before constructing the block-name lookup,
    while the full original name (with the abbreviation) is kept as the
    display name.
    """
    specialisations = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        text = heading.get_text(strip=True)
        match = SPECIALISATION_HEADING_PATTERN.match(text)
        if not match:
            continue

        spec_type = match.group(1).strip().lower()  # "stream" or "breadth"
        name = match.group(2).strip()
        name_for_lookup = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()

        core_key = f"Specialisation - {name_for_lookup} Core Units"
        elective_key = f"Specialisation - {name_for_lookup} Elective Units"

        specialisations.append({
            "name": name,
            "type": spec_type,
            "core_block": core_key if core_key in unit_blocks else None,
            "elective_block": elective_key if elective_key in unit_blocks else None,
        })

    return specialisations


def main():
    print(f"Fetching CUSP print view for degree_id={DEGREE_ID}...")
    soup = fetch_print_view()
    if soup is None:
        print("Aborting — could not fetch the page.")
        return

    unit_blocks = parse_unit_blocks(soup)
    semester_plan = parse_semester_plan(soup)
    specialisations = parse_specialisations(soup, unit_blocks)

    total_units = sum(len(b["units"]) for b in unit_blocks.values())
    print(f"Parsed {len(unit_blocks)} unit blocks, {total_units} total unit entries.")
    for name, block in unit_blocks.items():
        print(f"  - {name}: {len(block['units'])} units (min {block['min_cp']}cp / max {block['max_cp']}cp)")
    print(f"Parsed {len(semester_plan)} year/semester sections.")
    print(f"Parsed {len(specialisations)} specialisations:")
    for spec in specialisations:
        missing = []
        if spec["core_block"] is None:
            missing.append("core block not matched")
        if spec["elective_block"] is None:
            missing.append("elective block not matched")
        flag = f" [WARN: {', '.join(missing)}]" if missing else ""
        print(f"  - ({spec['type']}) {spec['name']}{flag}")

    if total_units == 0 or not semester_plan:
        print("\n[WARN] Got zero units or zero semester sections — the heading-tag or")
        print("table-column assumptions in parse_unit_blocks/parse_semester_plan almost")
        print("certainly need adjusting to match the real page markup. Inspect the raw")
        print("HTML (e.g. print(soup.prettify()[:3000])) and fix the tag names/column")
        print("indices above.")

    with open(OUTPUT_FILE, "w") as f:
        json.dump({
            "degree_id": DEGREE_ID,
            "source_url": PRINT_VIEW_URL.format(DEGREE_ID),
            "unit_blocks": unit_blocks,
            "semester_plan": semester_plan,
            "specialisations": specialisations,
        }, f, indent=2)

    print(f"Saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. This scrapes ALL required blocks (Introductory Computing, Foundation
#    Maths, Projects, PEP, Software Stream Core/Electives, Free Electives),
#    fixing the gap in stream_table_units.json, which only had the
#    Software Stream's own table (Blocks 4-6).
# 2. semester_plan is USYD's own official semester-by-semester structure —
#    this is worth treating as the ground-truth "default" plan for Path B,
#    with the DAG/prerequisite logic used to validate custom replans
#    (e.g. after a failed unit) against it rather than generating a plan
#    from raw prerequisite data alone.
# 3. Next step once this runs cleanly: merge unit_blocks' Software Stream
#    Core/Electives entries with stream_table_units.json's existing
#    prerequisite/corequisite/prohibition data (matched by unit code), so
#    the planner has both the full requirement set AND per-unit prereqs
#    in one place.
# 4. degree_id=454 is Software Engineering (2026) specifically — CUSP uses
#    the same page structure for every USYD degree, so this same scraper
#    (with a different degree_id) is how Path A's second-institution... well,
#    actually CUSP is USYD-only, so this doesn't help Path A's cross-
#    institution comparison, but it would help if you ever add another
#    USYD degree (e.g. comparing Software Eng vs Computer Science internally).