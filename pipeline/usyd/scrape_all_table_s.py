"""
Scrape all 7 Table S elective pages (one per faculty), each into its own
JSON file - table_s_<faculty>_units.json.

Table S is USYD's shared pool of electives: units from one faculty that
students in other degrees can take as free electives. CUSP's degree page
correctly shows 0 fixed units for "Free Electives" (see
cusp_degree_structure.json), since that requirement is genuinely open
("any Faculty of Engineering unit, or a Table S elective") rather than a
fixed list - but each Table S faculty page IS a real, bounded list with
its own prerequisite/corequisite/prohibition data.

Structural note - these pages are NOT all laid out the same way, and this
has needed fixing twice already:
  - Engineering: one page, one table.
  - Science: one page, multiple tables (one per subject area - Chemistry,
    Geosciences, etc.). scrape_stream_table() already handles this fine,
    since it loops over every <table> on the page rather than assuming
    there's exactly one.
  - Arts and Social Sciences: confirmed to go a step further. The page
    itself is just an INDEX with no unit table at all - 47 subject-area
    links (American Studies, Ancient Greek, ... Writing Studies), each
    presumably with its own real unit table on its own subpage.

Given that pattern has already changed twice across 3 checked pages, this
script no longer assumes a page will render units directly. If
scrape_stream_table() gets 0 units from a page, it treats that page as a
possible index, looks for links to same-directory subpages, and scrapes
each of those instead - combining everything into one result per faculty.
This should handle Business/Medicine and Health/Architecture without
needing to manually check each one first, though it's still worth
sanity-checking their output counts once this runs.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P
import os

import json
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from scale_live_fetch import scrape_stream_table, HEADERS

BASE_URL = "https://www.sydney.edu.au/handbooks/interdisciplinary-studies/table-s/electives/{}.html"

# key -> URL slug
TABLE_S_PAGES = {
    "architecture_design_planning": "architecture-design-planning-unit-of-study-table",
    "arts": "arts-unit-of-study-table",
    "business_school": "business-school-unit-of-study-table",
    "conservatorium": "conservatorium-unit-of-study-table",
    "engineering": "engineering-unit-of-study-table",
    "medicine_health": "medicine-health-unit-of-study-table",
    "science": "science-unit-of-study-table",
}

OUTPUT_FILE_TEMPLATE = os.path.join(os.path.dirname(P("table_s_arts_units.json")),
                                    "table_s_{}_units.json")

# Non-subject-area pages that show up alongside real subject links on an
# index page (confirmed present on the Arts index) - skip these if found,
# no unit table to gain from either.
INDEX_NOISE_SUFFIXES = ("/overview.html", "/electives.html")


def discover_subpages(index_url, timeout=15):
    """
    Look for links to same-directory subpages from a page that turned out
    to have no unit table of its own (an index/overview page, confirmed
    for Arts and Social Sciences). Returns a sorted list of subpage URLs,
    or [] if the fetch fails or none are found.

    Resolves every href against response.url (the page's actual URL after
    following any redirect) rather than comparing raw href strings
    directly - hrefs on the page are very likely root-relative
    (e.g. "/handbooks/.../american-studies.html") or even directory-
    relative (e.g. "american-studies.html"), not the fully-qualified
    absolute URLs a rendered/markdown view of the page might show.
    """
    try:
        response = requests.get(index_url, headers=HEADERS, timeout=timeout)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"  [ERROR] Couldn't fetch {index_url} to look for subpages: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    directory_prefix = index_url.rsplit(".html", 1)[0] + "/"

    subpages = set()
    for a in soup.find_all("a", href=True):
        absolute_href = urljoin(response.url, a["href"])
        if absolute_href.startswith(directory_prefix) and absolute_href.endswith(".html"):
            if not absolute_href.endswith(INDEX_NOISE_SUFFIXES):
                subpages.add(absolute_href)

    return sorted(subpages)


def scrape_table_s_page(key, url):
    """
    Scrape one Table S faculty page. If it yields 0 units directly, check
    whether it's actually an index page with subpages (confirmed pattern
    for Arts) and, if so, scrape each subpage and combine the results.
    """
    result = scrape_stream_table(table_url=url)
    units = result.get("units", [])

    if units:
        return units

    print(f"  Got 0 units directly from the main page - checking whether "
          f"{key} is an index page with subpages instead...")
    subpages = discover_subpages(url)

    if not subpages:
        print(f"  [WARN] No subpages found either - {key} may genuinely have 0 units on "
              f"Table S, or its layout differs in some other way not yet accounted for. "
              f"Worth checking https://www.sydney.edu.au{url.split('sydney.edu.au')[-1]} "
              f"manually if this is unexpected.")
        return []

    print(f"  Found {len(subpages)} subpages under this index - scraping each...")
    all_units = {}
    for sub_url in subpages:
        sub_result = scrape_stream_table(table_url=sub_url)
        for u in sub_result.get("units", []):
            all_units[u["code"]] = u  # dedupe by code across subpages
        time.sleep(0.5)

    return list(all_units.values())


def main():
    summary = {}

    for key, slug in TABLE_S_PAGES.items():
        url = BASE_URL.format(slug)
        print(f"Scraping Table S - {key} ({url})...")

        units = scrape_table_s_page(key, url)

        if not units:
            print(f"  [WARN] Ended up with 0 units for {key}.")
        else:
            print(f"  Got {len(units)} units total.")

        output_file = OUTPUT_FILE_TEMPLATE.format(key)
        with open(output_file, "w") as f:
            json.dump({"source_url": url, "units": units}, f, indent=2)
        print(f"  Saved to {output_file}")

        summary[key] = len(units)
        time.sleep(1)  # be polite between requests, same as scale_live_fetch.py

    print("\n=== Summary ===")
    total = 0
    for key, count in summary.items():
        print(f"  {key}: {count} units")
        total += count
    print(f"  TOTAL: {total} units across {len(TABLE_S_PAGES)} Table S pages")


if __name__ == "__main__":
    main()


# ---- Notes ----
# 1. Output is still one JSON file per faculty (table_s_arts_units.json
#    etc.), even though Arts' units were gathered from 47 subpages rather
#    than one page - the per-faculty granularity is what matters for
#    downstream use, not how many requests it took to assemble it.
# 2. If the summary printout shows 0 units for any faculty even after the
#    subpage fallback, that page's layout differs in some third way not
#    covered by "single table", "multiple tables on one page", or "index
#    of subpages" - worth fetching it directly to see what's actually
#    there rather than assuming the scraper is simply broken.
# 3. Next step: extend merge_degree_data.py to fold all 7 into
#    merged_degree_data.json's "Free Electives" pool, with a source-
#    faculty tag per unit.