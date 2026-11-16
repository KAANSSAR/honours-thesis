"""
Parse the C09066 suggested sequences from the cached course page HTML.

    python3 parse_uts_sequences_html.py

Reads uts_cache/ and writes uts_sequences.json. No network access, no PDF.

Replaces parse_uts_sequences.py, which read `pdftotext -layout` output and inferred
nesting from character offsets. Three reasons the HTML is the better source:

1. The PDF went missing twice and cost a rebuild both times. The cached HTML is
   fetched alongside everything else and cannot go missing on its own.

2. Nesting is explicit rather than inferred. Each row has three cells: the subject,
   then the credit points in the second cell if the row is an option under a
   "Select N credit points" group, or in the third cell if it is a main entry.

       <td>48310 Introduction to Civil...</td> <td>&nbsp;</td> <td>6cp</td>  main
       <td>48320 Surveying</td>                <td>6cp</td>    <td>&nbsp;</td>  option

   The PDF parser had to recover that distinction by finding the most common
   column offset of the credit-point figure and thresholding against it, which
   worked but is guesswork compared with reading which cell the value is in.

3. The PDF parser only kept sequences whose names were in a hardcoded map of seven
   majors, and silently dropped the rest. The course page actually publishes
   fifteen, including "Civil Engineering major, Structures specialisation" and
   "Civil Engineering major, Construction specialisation", which are per-
   specialisation sequences directly comparable to the USYD stream structure. They
   were discarded for weeks, and only surfaced because the omission was questioned.
   Every sequence is now parsed and kept; scope is applied afterwards and recorded,
   so what is excluded stays visible in the output.

Session headings are read as published, so a December or July session is picked up
without needing to be listed in advance, and anything that cannot be mapped to a
position is reported rather than absorbed into the preceding heading.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import glob, json, os, re, sys
from bs4 import BeautifulSoup

CACHE = P("uts_cache")
OUT = P("uts_sequences.json")
COURSE_URL = "https://handbookpre2025.uts.edu.au/2024/courses/c09066.html"

# Sequences kept in scope, mapped to the USYD degree they are compared against.
# The two Civil specialisations pair with the USYD Civil plans, which are built on
# the Structures specialisation, so they compare like with like.
MAJOR_MAP = {
    "Software Engineering major": "Software Engineering",
    "Biomedical Engineering major": "Biomedical",
    "Civil Engineering major": "Civil",
    "Civil Engineering major, Structures specialisation": "Civil",
    "Civil Engineering major, Construction specialisation": "Civil",
    "Civil and Environmental Engineering major": "Environmental",
    "Electrical Engineering major": "Electrical",
    "Mechanical Engineering major": "Mechanical",
    "Mechatronic Engineering major": "Mechatronic",
}
UNMATCHED_USYD = ["Aeronautical"]

SESSION_POSITION = {
    "Autumn": "sem1", "Spring": "sem2",
    "July": "winter_gap", "Winter": "winter_gap",
    "December": "summer_gap", "January": "summer_gap", "Summer": "summer_gap",
}

HEAD = re.compile(r"^(.*?),\s*(Autumn|Spring|Summer|January|July)\s+commencing$")
YEAR = re.compile(r"^Year\s+(\d+)$")
SESSION = re.compile(r"^([A-Z][a-z]+)\s+session$")
SELECT = re.compile(r"^Select\s+(\d+)\s+credit points?\s*(of options|from the following)?:?\s*$",
                    re.IGNORECASE)
SUBJECT = re.compile(r"^(\d{5})\s*(.*)$")
BLOCK = re.compile(r"^((?:CBK|STM|SMJ|MAJ)\d{5})\s*(.*)$", re.IGNORECASE)
CP = re.compile(r"^(\d+)\s*cp$", re.IGNORECASE)


def cell_text(td):
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).replace("\xa0", " ").strip()


def course_page():
    for p in (f"*courses_c09066_html", f"*c09066*"):
        hits = [h for h in (glob.glob(os.path.join(CACHE, p + ".html"))
                            or glob.glob(os.path.join(CACHE, p)))
                if "__MACOSX" not in h and os.path.getsize(h) > 10000]
        if hits:
            return sorted(hits)[0]
    return None


def parse(html):
    """Walk the rows in document order, tracking sequence, year and session."""
    soup = BeautifulSoup(html, "html.parser")
    sequences, order = {}, []
    current = year = session = None
    pending = None
    sessions_seen, unmapped_rows = set(), []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False) or tr.find_all("td")
        if not tds:
            continue
        first = cell_text(tds[0])
        if not first:
            continue

        # A spanning cell is a heading: the sequence, a year, or a session.
        if len(tds) == 1 or (tds[0].get("colspan") in ("3", 3)):
            m = HEAD.match(first)
            if m:
                current = m.group(1).strip()
                sequences.setdefault(current, [])
                order.append(current)
                year = session = pending = None
                continue
            m = YEAR.match(first)
            if m and current:
                year = int(m.group(1))
                pending = None
                continue
            m = SESSION.match(first)
            if m and current:
                session = m.group(1)
                sessions_seen.add(session)
                pending = None
                continue
            continue

        if current is None or year is None or session is None:
            continue

        # Which cell holds the credit points decides main entry vs nested option.
        main_cp = CP.match(cell_text(tds[2])) if len(tds) > 2 else None
        opt_cp = CP.match(cell_text(tds[1])) if len(tds) > 1 else None
        nested = opt_cp is not None and main_cp is None
        cp = int((main_cp or opt_cp).group(1)) if (main_cp or opt_cp) else None

        m = SELECT.match(first)
        if m and not nested:
            pending = {"kind": "choice", "year": year, "session": session,
                       "credit_points": int(m.group(1)),
                       "resolved_inline": bool(m.group(2) and
                                               "following" in m.group(2).lower()),
                       "text": first, "options": []}
            sequences[current].append(pending)
            continue

        bm = BLOCK.match(first)
        if bm:
            rec = {"code": bm.group(1).upper(), "name": bm.group(2).strip(),
                   "credit_points": cp if cp is not None else 6, "is_block": True}
            if nested and pending is not None:
                pending["options"].append(rec)
            continue

        sm = SUBJECT.match(first)
        if sm:
            code, name = sm.group(1), sm.group(2).strip()
            if nested and pending is not None:
                pending["options"].append({"code": code, "name": name,
                                           "credit_points": cp if cp is not None else 6})
            else:
                sequences[current].append(
                    {"kind": "fixed", "year": year, "session": session, "code": code,
                     "name": name, "credit_points": cp if cp is not None else 6})
                pending = None
            continue

        unmapped_rows.append({"sequence": current, "year": year,
                              "session": session, "text": first[:80]})
        pending = None

    return sequences, sessions_seen, unmapped_rows


def main():
    if not os.path.isdir(CACHE):
        sys.exit(f"{CACHE}/ not found. Run this from the folder holding uts_cache/.")
    path = course_page()
    if not path:
        sys.exit(f"course page not found in {CACHE}/ (looked for a c09066 file)")
    print(f"reading {os.path.basename(path)}")

    sequences, sessions_seen, unmapped = parse(
        open(path, encoding="utf-8", errors="ignore").read())

    kept = {k: v for k, v in sequences.items() if k in MAJOR_MAP}
    missing = sorted(set(MAJOR_MAP) - set(sequences))
    excluded = sorted(set(sequences) - set(MAJOR_MAP))

    subjects = sorted({e["code"] for v in kept.values() for e in v if e["kind"] == "fixed"}
                      | {o["code"] for v in kept.values() for e in v
                         if e["kind"] == "choice" for o in e["options"]
                         if not o.get("is_block")})
    blocks = sorted({o["code"] for v in kept.values() for e in v if e["kind"] == "choice"
                     for o in e["options"] if o.get("is_block")}
                    | {"CBK90011", "STM90848", "STM90849", "STM91414",
                       "STM90106", "STM90994"})
    unresolved = [{"major": k, "year": e["year"], "session": e["session"],
                   "credit_points": e["credit_points"], "text": e["text"]}
                  for k, v in kept.items() for e in v
                  if e["kind"] == "choice" and not e["resolved_inline"]]

    out = {"institution": "UTS", "course_code": "C09066v6",
           "course_name": "Bachelor of Engineering (Honours)",
           "total_credit_points": 198,
           "source": "handbookpre2025.uts.edu.au 2024 edition (course page HTML)",
           "source_url": COURSE_URL,
           "parsed_from": "cached course page HTML, three-column rows",
           "structure": {"core": 48, "major": 120,
                         "electives_or_specialist_stream": 24,
                         "professional_practice": 6},
           "major_map_to_usyd": MAJOR_MAP,
           "unmatched_usyd_degrees": UNMATCHED_USYD,
           "all_sequence_names": sorted(sequences),
           "excluded_sequences": excluded,
           "session_position_map": SESSION_POSITION,
           "sessions_seen": sorted(sessions_seen),
           "sessions_unmapped": sorted(sessions_seen - set(SESSION_POSITION)),
           "rows_not_understood": unmapped,
           "sequences": kept,
           "subjects_to_fetch": subjects,
           "blocks_to_fetch": blocks,
           "unresolved_choice_groups": unresolved}
    json.dump(out, open(OUT, "w"), indent=1)

    print(f"\n{len(sequences)} sequences on the page, {len(kept)} in scope")
    if missing:
        print(f"  [WARN] in scope but not found: {missing}")
    print(f"  excluded (no USYD counterpart): {len(excluded)}")
    for e in excluded:
        print(f"      {e}")
    print(f"\n  sessions seen: {sorted(sessions_seen)}")
    if out["sessions_unmapped"]:
        print(f"  [WARN] no position mapped for: {out['sessions_unmapped']}")
    if unmapped:
        print(f"  [WARN] {len(unmapped)} rows not understood, e.g. {unmapped[:3]}")

    print(f"\n{'sequence':48}{'fixed':>7}{'choice':>8}{'options':>9}{'cp':>7}")
    for k in sorted(kept):
        v = kept[k]
        f = sum(1 for e in v if e["kind"] == "fixed")
        c = sum(1 for e in v if e["kind"] == "choice")
        o = sum(len(e["options"]) for e in v if e["kind"] == "choice")
        cp = sum(e.get("credit_points", 0) for e in v)
        print(f"{k[:46]:48}{f:>7}{c:>8}{o:>9}{cp:>5}cp")
    print(f"\n{len(subjects)} subject pages, {len(blocks)} block pages, "
          f"{len(unresolved)} unresolved choice groups")
    print(f"wrote {OUT}")

    bad = [k for k, v in kept.items()
           if sum(e.get("credit_points", 0) for e in v) != out["total_credit_points"]]
    if bad:
        print(f"\n  [WARN] not summing to {out['total_credit_points']}cp: {bad}")
    else:
        print(f"\n  every sequence sums to {out['total_credit_points']}cp straight from "
              f"the page.")
        print("  Two corrections the PDF route needed are therefore unnecessary here, and")
        print("  reparse_uts2.py will now report both as redundant rather than applying")
        print("  them: 41028's 0cp is stated outright in its own cell, and 48850 sits")
        print("  under its December session heading where the page puts it.")


if __name__ == "__main__":
    main()
