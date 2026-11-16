"""
Fetch UTS C09066 subject and block pages and merge them into uts_c09066_data.json.

The sequences are already parsed from the course-page PDF (uts_sequences.json), so
this script only fetches pages. It is safe to stop and re-run: pages are cached on
disk and progress is saved after every page, so a timeout costs one page, not the run.

    python3 fetch_uts_pages.py                 # fetch everything still missing
    python3 fetch_uts_pages.py --retry-failed  # re-attempt only the pages that failed

Needs: requests, beautifulsoup4
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import json, os, re, sys, time
import requests
from bs4 import BeautifulSoup

BASE = "https://handbookpre2025.uts.edu.au/2024"
SEQ_IN = P("uts_sequences.json")
OUT = P("uts_c09066_data.json")
CACHE = P("uts_cache")
HEADERS = {"User-Agent": "Mozilla/5.0 (honours research; contact via UTS student)"}
TIMEOUT = 30
PAUSE = 0.5
RETRIES = 3
BACKOFF = 4


def cache_path(url):
    return os.path.join(CACHE, re.sub(r"[^A-Za-z0-9]+", "_", url)[-120:] + ".html")


def fetch(url):
    """Return page HTML, or None after exhausting retries. Never raises."""
    os.makedirs(CACHE, exist_ok=True)
    cp = cache_path(url)
    if os.path.exists(cp) and os.path.getsize(cp) > 500:
        return open(cp, encoding="utf-8", errors="ignore").read()
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and len(r.text) > 500:
                open(cp, "w", encoding="utf-8").write(r.text)
                time.sleep(PAUSE)
                return r.text
            print(f"    HTTP {r.status_code} ({len(r.text)} bytes)")
        except requests.RequestException as e:
            print(f"    attempt {attempt}/{RETRIES}: {type(e).__name__}")
        if attempt < RETRIES:
            time.sleep(BACKOFF * attempt)
    return None


def flat(soup):
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def parse_subject(code, html):
    body = flat(BeautifulSoup(html, "html.parser"))
    rec = {"code": code, "name": "", "credit_points": None, "sessions_offered": [],
           "prerequisite_codes": [], "prerequisite_groups": [], "corequisite_codes": [],
           "antirequisite_codes": [], "credit_point_requirement": None,
           "requirements_raw": "", "institution": "UTS",
           "source_url": f"{BASE}/subjects/{code}.html"}

    nm = re.search(rf"{code}\s+([A-Z][^|]{{2,90}}?)(?:\s+\d+cp|\s+Warning|\s+Requisite|$)", body)
    if nm:
        rec["name"] = nm.group(1).strip()
    cpm = re.search(r"\b(\d+)cp\b", body)
    if cpm:
        rec["credit_points"] = int(cpm.group(1))

    rec["sessions_offered"] = sorted({f"{m.group(1)} session" for m in
                                      re.finditer(r"\b(Autumn|Spring|Summer)\s+session", body)})

    stop = (r"(?=Prerequisite|Corequisite|Anti-requisite|Requisite elaboration|"
            r"Recommended|Description|Subject objectives|Fee|Availability|Assessment|$)")
    for label, field in (("Prerequisite", "prerequisite_codes"),
                         ("Corequisite", "corequisite_codes"),
                         ("Anti-requisite", "antirequisite_codes")):
        mm = re.search(rf"{label}\(?s?\)?:\s*(.+?){stop}", body, re.IGNORECASE)
        if not mm:
            continue
        clause = mm.group(1).strip()[:1500]
        if not clause or clause.lower().startswith("there are no"):
            continue
        rec["requirements_raw"] += f"{label}: {clause}\n"
        rec[field] = sorted(set(re.findall(r"\b(\d{5})\b", clause)))
        if field == "prerequisite_codes":
            groups = []
            for part in re.split(r"\bAND\b", clause, flags=re.IGNORECASE):
                g = sorted(set(re.findall(r"\b(\d{5})\b", part)))
                if g:
                    groups.append(g)
            rec["prerequisite_groups"] = groups
            cpr = re.search(r"(\d+)\s*credit points?\s+of\s+([^.;]{0,80})", clause, re.IGNORECASE)
            if cpr:
                rec["credit_point_requirement"] = {"min_credit_points": int(cpr.group(1)),
                                                   "scope": cpr.group(2).strip(),
                                                   "raw": clause[:300]}
    return rec


def parse_block(code, html):
    soup = BeautifulSoup(html, "html.parser")
    body = flat(soup)
    rec = {"code": code.upper(), "name": "", "max_cp": None, "units": [],
           "sub_blocks": [], "institution": "UTS",
           "source_url": f"{BASE}/directory/{code.lower()}.html"}
    nm = re.search(rf"{code}\s+([A-Z][^|]{{2,90}}?)(?:\s+\d+cp|\s+The following|$)", body, re.IGNORECASE)
    if nm:
        rec["name"] = nm.group(1).strip()
    cpm = re.search(r"\b(\d+)cp\b", body)
    if cpm:
        rec["max_cp"] = int(cpm.group(1))
    for a in soup.find_all("a", href=True):
        t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        parent = re.sub(r"\s+", " ", a.parent.get_text(" ", strip=True)) if a.parent else t
        cm = re.match(r"^(\d{5})\b\s*(.*)$", t)
        if cm and "/subjects/" in a["href"]:
            cp = re.search(r"\b(\d+)cp\b", parent)
            rec["units"].append({"code": cm.group(1), "name": cm.group(2).strip(),
                                 "credit_points": int(cp.group(1)) if cp else 6})
        bm = re.match(r"^((?:CBK|STM|SMJ|MAJ)\d{5})\b", t, re.IGNORECASE)
        if bm and "/directory/" in a["href"]:
            rec["sub_blocks"].append(bm.group(1).upper())
    seen = set()
    rec["units"] = [u for u in rec["units"] if not (u["code"] in seen or seen.add(u["code"]))]
    rec["sub_blocks"] = sorted(set(rec["sub_blocks"]) - {rec["code"]})
    return rec


def main():
    retry_failed = "--retry-failed" in sys.argv
    if not os.path.exists(SEQ_IN):
        sys.exit(f"{SEQ_IN} not found. It should have come with this script.")
    seq = json.load(open(SEQ_IN))

    data = json.load(open(OUT)) if os.path.exists(OUT) else dict(seq)
    data.setdefault("units", {})
    data.setdefault("blocks", {})
    data.setdefault("failed", [])
    if retry_failed:
        data["failed"] = []

    subjects = list(seq["subjects_to_fetch"])
    queue = list(seq["blocks_to_fetch"])
    failed = set(data["failed"]) if not retry_failed else set()

    print(f"Blocks: {len(queue)} seeded")
    seen_blocks = set()
    while queue:
        code = queue.pop(0).upper()
        if code in seen_blocks or code in data["blocks"]:
            seen_blocks.add(code)
            continue
        seen_blocks.add(code)
        print(f"  {code}")
        html = fetch(f"{BASE}/directory/{code.lower()}.html")
        if not html:
            failed.add(code)
            continue
        rec = parse_block(code, html)
        data["blocks"][code] = rec
        for sb in rec["sub_blocks"]:
            if sb not in seen_blocks:
                queue.append(sb)
        for u in rec["units"]:
            if u["code"] not in subjects:
                subjects.append(u["code"])
        data["failed"] = sorted(failed)
        json.dump(data, open(OUT, "w"), indent=1)

    print(f"\nSubjects: {len(subjects)} to fetch")
    for i, code in enumerate(sorted(set(subjects)), 1):
        if code in data["units"]:
            continue
        html = fetch(f"{BASE}/subjects/{code}.html")
        if not html:
            failed.add(code)
            continue
        data["units"][code] = parse_subject(code, html)
        data["failed"] = sorted(failed)
        if i % 10 == 0:
            json.dump(data, open(OUT, "w"), indent=1)
            print(f"  {i}/{len(subjects)}")
    json.dump(data, open(OUT, "w"), indent=1)

    units = data["units"]
    no_sess = sorted(c for c, u in units.items() if not u["sessions_offered"])
    no_cp = sorted(c for c, u in units.items() if not u["credit_points"])
    no_req = sorted(c for c, u in units.items() if not u["requirements_raw"])
    data["data_gaps"] = {"subjects_without_sessions": no_sess,
                         "subjects_without_credit_points": no_cp,
                         "subjects_without_any_requisite_text": no_req,
                         "unresolved_choice_groups": seq["unresolved_choice_groups"]}
    json.dump(data, open(OUT, "w"), indent=1)

    print(f"\nWrote {OUT}")
    print(f"  sequences: {len(data['sequences'])}   blocks: {len(data['blocks'])}   "
          f"subjects: {len(units)}")
    print(f"  missing sessions: {len(no_sess)}   missing credit points: {len(no_cp)}")
    print(f"  no requisite text at all: {len(no_req)}")
    print(f"  unresolved choice groups: {len(seq['unresolved_choice_groups'])}")
    if failed:
        print(f"\n  FAILED ({len(failed)}): {sorted(failed)}")
        print("  re-run with --retry-failed once your connection is stable")
    print("\nCheck these counts before trusting the output: scraper faults on this "
          "site show up as empty lists, not errors.")


if __name__ == "__main__":
    main()
