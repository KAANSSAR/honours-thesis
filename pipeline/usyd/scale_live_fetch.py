"""
Scaled live-fetch pipeline for USYD handbook units.

Two stages:
  1. Scrape the Software Engineering stream unit-of-study table to get a
     real list of unit codes (with credit points and raw requirements
     text), instead of a hardcoded list.
  2. For each scraped unit code, fetch its individual handbook page and
     pass it to Qwen3-8B (via Ollama) for structured extraction — same
     as the original INFO1110 proof of concept, now run at scale.

Includes structured error handling at both stages and writes results to
JSON, per supervisor feedback.

Designed to run locally (as tested) or be lifted into Google Colab — see
the note at the bottom of this file for the Colab adjustments.
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
import time

# ---- Configuration ----
STREAM_TABLE_URL = "https://www.sydney.edu.au/handbooks/engineering/engineering-honours/streams/software/unit-of-study-table.html"
UNIT_PAGE_URL = "https://www.sydney.edu.au/units/{}"
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "qwen3:8b"

STREAM_TABLE_OUTPUT_FILE = P("stream_table_units.json")
HANDBOOK_RESULTS_FILE = P("handbook_results.json")

HEADERS = {"User-Agent": "Mozilla/5.0"}

# Fallback list, only used if the table scrape returns nothing (e.g. the
# page structure changes and the scraper needs updating).
FALLBACK_UNIT_CODES = ["INFO1110", "INFO1113", "COMP2017", "COMP3027", "INFO2222"]

UNIT_LINK_PATTERN = re.compile(r"/units/([A-Z0-9]+)", re.IGNORECASE)

# Maps the bold single-letter label used in the requirements column to a
# readable field name. Per the table's convention:
#   A = Assumed knowledge, P = Prerequisites, C = Corequisites, N = Prohibitions
# Not every unit has all four — some have none at all.
REQUIREMENT_LABELS = {
    "A": "assumed_knowledge",
    "P": "prerequisites",
    "C": "corequisites",
    "N": "prohibitions",
}

# Keywords that flip a code from "one of the required alternatives" to
# "explicitly excluded from counting" - see parse_requirements_cell().
EXCLUSION_KEYWORDS = ("except", "excluding")

# Word-boundary match for "and" as a group-boundary signal - see
# parse_requirements_cell(). Word-boundary avoids false hits inside
# unrelated words (e.g. "Understanding").
AND_KEYWORD_PATTERN = re.compile(r"\band\b", re.IGNORECASE)


def parse_requirements_cell(cell):
    """
    Walk a requirements cell (the 3rd column) in FULL document order,
    including the plain text between tags, not just the tags themselves.
    Each bold single-letter marker (A/P/C/N) introduces a section; every
    linked unit code that follows belongs to that section, until the
    next marker.

    Handles "except (...)" / "excluding (...)" clauses - e.g. MATH2069's
    real requirement text is "P 6 credit points of MATH1XXX except
    (MATH1XX5 or MATH1050 or MATH1111)": those three codes are explicitly
    carved OUT of a broader wildcard requirement, not alternative ways to
    satisfy it. Codes found after an exclusion keyword (until the next
    marker resets it) are collected in a separate `excluded_codes` dict
    instead of the section's normal list.

    ALSO handles AND-grouping - e.g. ELEC3609's real prerequisite is
    "(INFO1103 or INFO1110 or INFO1910 or ENGG1810) and (INFO2110 or
    ISYS2110) and (INFO2120 or INFO2820 or ISYS2120)": three SEPARATE
    groups that must ALL be satisfied, each satisfiable by any ONE of its
    own alternatives - not one flat 8-code list where any single code
    would wrongly be treated as sufficient (the previous version's
    behaviour, which let a student's plan schedule ELEC3609 as early as
    right after INFO1110 alone, when it also genuinely needs ISYS2110 and
    one of INFO2120/INFO2820/ISYS2120). The result's '<section>_groups'
    key holds a list of OR-groups (each a list of codes) per section -
    every group must have at least one satisfied member. The flat
    '<section>' list is still populated too (the union of all codes,
    regardless of grouping), since dependency/cascade detection doesn't
    care about AND/OR structure, only "does this code appear at all".

    KNOWN SIMPLIFICATIONS:
      - Once an exclusion keyword appears, every code found afterward is
        treated as excluded until the next marker - no attempt to detect
        the exclusion's own scope ending earlier (e.g. via a closing
        parenthesis). Matches every example seen so far, where
        "except (...)" is the last thing in its clause.
      - AND-grouping doesn't track parenthesis nesting - "and" is treated
        as the sole group-boundary signal, which correctly reconstructs
        every real-world example seen so far (flat, unnested parenthesised
        clusters joined by "and"), but wouldn't correctly handle a
        genuinely nested case like "(A and (B or C))". No such case has
        been observed in this handbook's requirements text.
    """
    result = {
        "assumed_knowledge": [],
        "prerequisites": [],
        "corequisites": [],
        "prohibitions": [],
        "unlabeled": [],
    }
    groups = {
        "assumed_knowledge": [],
        "prerequisites": [],
        "corequisites": [],
        "prohibitions": [],
        "unlabeled": [],
    }
    excluded = {
        "assumed_knowledge": [],
        "prerequisites": [],
        "corequisites": [],
        "prohibitions": [],
        "unlabeled": [],
    }
    current_key = "unlabeled"
    excluding = False
    current_group = []

    def close_group(section):
        nonlocal current_group
        if current_group:
            groups[section].append(current_group)
        current_group = []

    for node in cell.descendants:
        tag_name = getattr(node, "name", None)

        if tag_name in ("strong", "b"):
            label_text = node.get_text(strip=True)
            if label_text in REQUIREMENT_LABELS:
                close_group(current_key)
                current_key = REQUIREMENT_LABELS[label_text]
                excluding = False  # new clause - exclusion doesn't carry over
            continue

        if tag_name == "a":
            match = UNIT_LINK_PATTERN.search(node.get("href", ""))
            if match:
                code = match.group(1).upper()
                if excluding:
                    excluded[current_key].append(code)
                else:
                    result[current_key].append(code)
                    current_group.append(code)
            continue

        # Plain text node - check for an exclusion keyword (flips how
        # subsequent codes are treated) or an "and" group boundary
        # (closes the current OR-group and starts a new one). Ignore
        # "and" while inside an exclusion clause - it doesn't define a
        # new inclusion group there. (NavigableString has no .name, so
        # tag_name is None here and it falls through to this branch.)
        if isinstance(node, str):
            if any(kw in node.lower() for kw in EXCLUSION_KEYWORDS):
                excluding = True
            elif not excluding and AND_KEYWORD_PATTERN.search(node):
                close_group(current_key)

    close_group(current_key)

    result["excluded_codes"] = {k: v for k, v in excluded.items() if v}
    result["groups"] = {k: v for k, v in groups.items() if v}
    return result


# ---------------------------------------------------------------------
# Stage 1: scrape the stream table for a real list of units
# ---------------------------------------------------------------------

def scrape_stream_overview(soup):
    """
    Pull the descriptive overview table that sits above the main unit
    table — e.g. the stream name and its credit-point breakdown
    ("Students complete 120 credit points... comprising: (a) 90 credit
    points of Core units, (b) ..."). This table has a single cell per
    row (unlike the main unit table's 2-3 cells per row), which is how
    we tell the two apart.

    Returns {"heading": str, "description_lines": [str, ...]} or None.
    """
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        cell_counts = [len(row.find_all("td")) for row in rows]
        if all(c == 1 for c in cell_counts):
            lines = [row.find("td").get_text(strip=True) for row in rows]
            return {
                "heading": lines[0] if lines else None,
                "description_lines": lines[1:],
            }
    return None


def scrape_stream_table(table_url=STREAM_TABLE_URL, timeout=10):
    """
    Scrape a USYD handbook unit-of-study table page.

    Returns {"stream_info": {...} or None, "units": [...]}. On failure,
    returns {"stream_info": None, "units": []} rather than raising, so
    callers can fall back gracefully.
    """
    try:
        response = requests.get(table_url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print("  [ERROR] Timeout fetching stream table page")
        return {"stream_info": None, "units": []}
    except requests.exceptions.HTTPError:
        print(f"  [ERROR] HTTP error fetching stream table: status {response.status_code}")
        return {"stream_info": None, "units": []}
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] Request failed fetching stream table: {e}")
        return {"stream_info": None, "units": []}

    soup = BeautifulSoup(response.text, "html.parser")
    stream_info = scrape_stream_overview(soup)
    units = []
    seen_codes = set()

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue  # header/divider rows (e.g. "Stream Core units")

            first_cell = cells[0]
            link = first_cell.find("a", href=UNIT_LINK_PATTERN)
            if not link:
                continue  # not a unit row

            match = UNIT_LINK_PATTERN.search(link.get("href", ""))
            if not match:
                continue
            code = match.group(1).upper()

            if code in seen_codes:
                continue  # same unit can appear in prereqs of another row too
            seen_codes.add(code)

            full_text = first_cell.get_text(separator=" ", strip=True)
            name = full_text.replace(code, "", 1).strip(" -")

            credit_points = None
            cp_text = cells[1].get_text(strip=True)
            if cp_text.isdigit():
                credit_points = int(cp_text)

            requirements_raw = ""
            requirements = {
                "assumed_knowledge": [],
                "prerequisites": [],
                "corequisites": [],
                "prohibitions": [],
                "unlabeled": [],
                "excluded_codes": {},
                "groups": {},
            }
            if len(cells) > 2:
                requirements_raw = cells[2].get_text(separator=" ", strip=True)
                requirements = parse_requirements_cell(cells[2])

            units.append({
                "code": code,
                "name": name,
                "credit_points": credit_points,
                "requirements_raw": requirements_raw,
                "assumed_knowledge_codes": requirements["assumed_knowledge"],
                "prerequisite_codes": requirements["prerequisites"],
                "prerequisite_groups": requirements.get("groups", {}).get("prerequisites", []),
                "corequisite_codes": requirements["corequisites"],
                "corequisite_groups": requirements.get("groups", {}).get("corequisites", []),
                "prohibition_codes": requirements["prohibitions"],
                "unlabeled_codes": requirements["unlabeled"],
                "excluded_codes": requirements.get("excluded_codes", {}),
            })

    return {"stream_info": stream_info, "units": units}


# ---------------------------------------------------------------------
# Stage 2: fetch each unit's individual handbook page + query the LLM
# ---------------------------------------------------------------------

def fetch_unit_page(unit_code, timeout=10):
    """Fetch a USYD individual unit handbook page. Returns (text, error)."""
    url = UNIT_PAGE_URL.format(unit_code)
    try:
        response = requests.get(url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        print(f"  [ERROR] Timeout fetching {unit_code}")
        return None, "timeout"
    except requests.exceptions.HTTPError:
        print(f"  [ERROR] HTTP error for {unit_code}: status {response.status_code}")
        return None, f"http_error_{response.status_code}"
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] Request failed for {unit_code}: {e}")
        return None, "request_failed"

    soup = BeautifulSoup(response.text, "html.parser")
    raw_text = soup.get_text(separator="\n", strip=True)

    if not raw_text or len(raw_text) < 100:
        print(f"  [WARN] Little/no content for {unit_code} — possible layout change or invalid code")
        return None, "empty_content"

    return raw_text[:8000], None


def query_llm(handbook_content, unit_code, timeout=120):
    """
    Send handbook content to Qwen3-8B via Ollama, asking only for semester
    availability. Everything else (code, name, credit points, prereqs,
    coreqs, prohibitions) is already known from the Stage 1 table scrape —
    there's no reason to make the model re-derive data you already have
    cleanly structured. Returns (text, error).
    """
    prompt = f"""The following is content fetched live from the University of Sydney
handbook for unit {unit_code}:

{handbook_content}

Based only on the information above, answer in one short line: what
semester(s) is this unit available in? If not stated, say "Not found".
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={"model": MODEL_NAME, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        print("  [ERROR] Could not connect to Ollama — is 'ollama serve' running?")
        return None, "ollama_connection_error"
    except requests.exceptions.Timeout:
        print(f"  [ERROR] Ollama timed out for {unit_code}")
        return None, "ollama_timeout"
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] Ollama request failed for {unit_code}: {e}")
        return None, "ollama_request_failed"

    try:
        result = response.json()
        return result.get("response", "").strip(), None
    except json.JSONDecodeError:
        print(f"  [ERROR] Could not parse Ollama JSON response for {unit_code}")
        return None, "invalid_json_response"


def main():
    # ---- Stage 1 ----
    print("Scraping unit list from Software Engineering stream table...")
    scrape_result = scrape_stream_table()
    stream_info = scrape_result.get("stream_info")
    table_units = scrape_result.get("units", [])

    if table_units:
        print(f"Found {len(table_units)} units in the stream table.")
        if stream_info and stream_info.get("heading"):
            print(f"Stream: {stream_info['heading']}")
        with open(STREAM_TABLE_OUTPUT_FILE, "w") as f:
            json.dump({"stream_info": stream_info, "units": table_units}, f, indent=2)
        print(f"Stream table data saved to {STREAM_TABLE_OUTPUT_FILE}")
        unit_codes = [u["code"] for u in table_units]
        units_by_code = {u["code"]: u for u in table_units}
    else:
        print("Stream table scrape returned nothing — falling back to a manual unit list.")
        unit_codes = FALLBACK_UNIT_CODES
        units_by_code = {}

    # Optional: limit how many units to run through the LLM step while
    # testing, since running all ~120 units through an 8B model locally
    # will take a while. Set to None to process everything. Note this
    # limit only affects the semester-lookup step below — the full table
    # scrape (all units, with prereqs/coreqs/prohibitions) already
    # happened above and is complete regardless of LIMIT.
    LIMIT = None
    if LIMIT:
        print(f"(Limiting semester lookup to the first {LIMIT} units for this run — adjust LIMIT to change.)")
        unit_codes = unit_codes[:LIMIT]

    # ---- Stage 2 ----
    results = []
    for unit_code in unit_codes:
        print(f"\nProcessing {unit_code}...")

        # Start from whatever Stage 1 already found for this unit, so we
        # don't throw away known-good structured data.
        record = dict(units_by_code.get(unit_code, {"code": unit_code}))
        record["semester_availability"] = None
        record["status"] = "pending"
        record["error"] = None

        handbook_content, fetch_error = fetch_unit_page(unit_code)
        if fetch_error:
            record["status"] = "failed"
            record["error"] = f"fetch:{fetch_error}"
            results.append(record)
            continue

        print(f"  Fetched OK. Sending to {MODEL_NAME}...")
        llm_response, llm_error = query_llm(handbook_content, unit_code)
        if llm_error:
            record["status"] = "failed"
            record["error"] = f"llm:{llm_error}"
            results.append(record)
            continue

        print("  Success.")
        record["semester_availability"] = llm_response
        record["status"] = "success"
        results.append(record)

        time.sleep(1)  # be polite to the server between requests

    with open(HANDBOOK_RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

    succeeded = sum(1 for r in results if r["status"] == "success")
    print(f"\nDone. {succeeded}/{len(unit_codes)} units processed successfully.")
    print(f"Results saved to {HANDBOOK_RESULTS_FILE}")
    print(f"(Full stream table with prereqs/coreqs/prohibitions for all {len(table_units) if table_units else len(FALLBACK_UNIT_CODES)} units is in {STREAM_TABLE_OUTPUT_FILE}, regardless of LIMIT.)")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. FIX (this version): parse_requirements_cell() previously only
#    inspected <strong>/<b>/<a> tags and never looked at the plain text
#    between them - so it had no way to see qualifier words like
#    "except", and silently added excluded codes (e.g. MATH2069's
#    "except (MATH1050 or MATH1111)") to prerequisites as if they were
#    required. It now walks the cell's full contents (text included) via
#    .descendants, and routes codes found after "except"/"excluding" into
#    a new excluded_codes field instead. See the function's docstring for
#    the known simplification this still carries.
# 2. IMPORTANT: this fix only changes the PARSER - it doesn't retroactively
#    fix stream_table_units.json or any table_s_*.json file already on
#    disk. Re-run this script, then scrape_all_table_s.py, then
#    merge_degree_data.py, to get the corrected data flowing through to
#    merged_degree_data.json.
# 3. The stream-table scraper (Stage 1) is generic table-parsing logic —
#    if USYD changes the table's HTML structure, this may need updating.
#    stream_table_units.json is worth inspecting after your first run to
#    confirm the codes, names, and credit points look right.
# 4. LIMIT only affects Stage 2 (the semester lookup). The full table
#    scrape — all ~120 units, with prerequisites/corequisites/prohibitions
#    already structured — happens regardless of LIMIT and is complete
#    the moment stream_table_units.json is written.
# 5. Stage 2's prompt was deliberately shrunk to ask for ONLY semester
#    availability, since code/name/credit points/prereqs/coreqs/
#    prohibitions are already known from Stage 1 — there's no reason to
#    make the model re-derive data you already have. This is most of
#    where the earlier slowness came from: asking for 6 fields when only
#    1 was actually unknown.
# 6. handbook_results.json now contains the FULL merged record per unit
#    (everything from the table scrape, plus semester_availability from
#    the LLM) rather than just the LLM's raw response.
# 7. Important distinction for your writeup: this batch script (scrape
#    ~120 units, run each through the LLM) is a validation tool to prove
#    the pipeline generalizes across the whole stream — it's not what a
#    student using the finished system would experience. In the deployed
#    system, live-fetch only runs for the specific unit(s) a student's
#    query actually needs — a handful of calls, a few seconds each. The
#    123-unit batch run is meant to be run once, offline, by you — not
#    something an end user waits on.
# 8. stream_table_units.json's top-level shape is now
#    {"stream_info": {...} or None, "units": [...]} rather than a bare
#    list — stream_info holds the descriptive overview text (degree/
#    stream name, credit-point structure) that sits above the main
#    table, captured separately since it has a different row shape
#    (one cell per row) than the unit rows (2-3 cells per row).
#
# ---- Moving this to Colab ----
# - requests and beautifulsoup4 are already available in Colab by default.
# - Ollama isn't preinstalled — install it in the Colab runtime
#   (curl -fsSL https://ollama.com/install.sh | sh), start it with
#   `ollama serve &`, then `ollama pull qwen3:8b`.
# - Alternative: skip Ollama in Colab and load the model directly via
#   Hugging Face `transformers` on Colab's GPU — often faster, avoids
#   the install step. The overall structure (scrape -> fetch -> query_llm
#   -> JSON output) stays the same either way.