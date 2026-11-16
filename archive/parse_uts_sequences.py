"""
Parse the C09066 suggested sequences out of the course-page PDF text.

The PDF uses column position to distinguish a sequence entry from the alternatives
listed under a "Select N credit points from the following" group: main entries put
their credit points further right than nested alternatives do. That is the only
reliable signal, so this parser works on `pdftotext -layout` output rather than HTML.

    pdftotext -layout c09066.pdf c09066.txt
    python3 parse_uts_sequences.py c09066.txt uts_sequences.json
"""
import json, re, sys
from collections import Counter

SRC, OUT = sys.argv[1], sys.argv[2]

# UTS runs more than the three teaching sessions. C09066 places 48850
# Environmental Planning and Law in a December session, and an earlier version of
# this parser only recognised Autumn, Spring and Summer. A "December session"
# heading therefore failed to match, the previous heading stayed in force, and
# 48850 was recorded in Year 3 Spring. That put Civil and Environmental at 30cp in
# Y3 Spring against 21cp in Y4 Autumn while every other major sat at 24 or 27, and
# it also gave 48850 a wrong inferred availability downstream. Any "<Name> session"
# heading is now accepted, and unknown names are reported rather than absorbed.
#
# Positions mirror the USYD model: the two semesters are teaching positions and
# everything else is an intensive sitting in a gap between them.
SESSION_POSITION = {
    "Autumn": "sem1", "Spring": "sem2",
    "July": "winter_gap", "Winter": "winter_gap",
    "December": "summer_gap", "January": "summer_gap", "Summer": "summer_gap",
}

MAJOR_MAP = {
    "Software Engineering major": "Software Engineering",
    "Biomedical Engineering major": "Biomedical",
    "Civil Engineering major": "Civil",
    "Civil and Environmental Engineering major": "Environmental",
    "Electrical Engineering major": "Electrical",
    "Mechanical Engineering major": "Mechanical",
    "Mechatronic Engineering major": "Mechatronic",
}
UNMATCHED_USYD = ["Aeronautical"]

raw = open(SRC, encoding="utf-8", errors="ignore").read().split("\n")

# Column of the credit-point figure tells a main entry from a nested alternative.
cp_cols = Counter()
for ln in raw:
    m = re.search(r"(\d+)cp\s*$", ln)
    if m and re.match(r"^\s*(\d{5}|Select)\b", ln.strip()):
        cp_cols[m.start()] += 1
if not cp_cols:
    sys.exit("No credit-point columns found. Was the text produced with -layout?")
# The main column is the most COMMON one, not the right-most: a handful of lines in
# the course-completion section sit further right and would otherwise set the mark.
MAIN_COL = cp_cols.most_common(1)[0][0]
THRESHOLD = MAIN_COL - 4

sequences, current, year, session, pending = {}, None, None, None, None
sessions_seen = set()


def push(entry):
    sequences[current].append(entry)


for ln in raw:
    s = ln.strip()
    if not s or s.startswith("https://") or "UTS Handbook" in s:
        continue

    if s.endswith("Autumn commencing"):
        current = s.replace(", Autumn commencing", "").strip()
        sequences.setdefault(current, [])
        year = session = pending = None
        continue
    if current is None:
        continue

    ym = re.match(r"^Year\s+(\d)$", s)
    if ym:
        year = int(ym.group(1))
        pending = None
        continue
    sm = re.match(r"^([A-Z][a-z]+)\s+session$", s)
    if sm:
        session = sm.group(1)
        sessions_seen.add(session)
        pending = None
        continue
    if year is None or session is None:
        continue

    cpm = re.search(r"(\d+)cp\s*$", ln)
    col = cpm.start() if cpm else None
    cp = int(cpm.group(1)) if cpm else None
    nested = col is not None and col < THRESHOLD

    om = re.match(r"^Select\s+(\d+)\s+credit points?\s*(of options|from the following)?:?\s*",
                  s, re.IGNORECASE)
    if om and not nested:
        pending = {"kind": "choice", "year": year, "session": session,
                   "credit_points": int(om.group(1)),
                   "resolved_inline": bool(om.group(2) and "following" in om.group(2).lower()),
                   "text": re.sub(r"\s*\d+cp\s*$", "", s), "options": []}
        push(pending)
        continue

    bm = re.match(r"^((?:CBK|STM|SMJ|MAJ)\d{5})\s+(.+?)\s*(?:\d+cp)?\s*$", s, re.IGNORECASE)
    if bm and nested and pending is not None:
        pending["options"].append({"code": bm.group(1).upper(), "name": bm.group(2).strip(),
                                   "credit_points": cp or 6, "is_block": True})
        continue

    cm = re.match(r"^(\d{5})\s+(.+?)\s*(?:\d+cp)?\s*$", s)
    if cm:
        code, name = cm.group(1), cm.group(2).strip()
        if nested and pending is not None:
            pending["options"].append({"code": code, "name": name,
                                       "credit_points": cp or 6})
        else:
            push({"kind": "fixed", "year": year, "session": session,
                  "code": code, "name": name, "credit_points": cp or 6})
            pending = None
        continue

    pending = None

kept = {k: v for k, v in sequences.items() if k in MAJOR_MAP}
missing = set(MAJOR_MAP) - set(sequences)

subjects = sorted({e["code"] for v in kept.values() for e in v if e["kind"] == "fixed"}
                  | {o["code"] for v in kept.values() for e in v if e["kind"] == "choice"
                     for o in e["options"] if not o.get("is_block")})
blocks = sorted({o["code"] for v in kept.values() for e in v if e["kind"] == "choice"
                 for o in e["options"] if o.get("is_block")}
                | {"CBK90011", "STM90848", "STM90849", "STM91414", "STM90106", "STM90994"})
unresolved = [{"major": k, "year": e["year"], "session": e["session"],
               "credit_points": e["credit_points"], "text": e["text"]}
              for k, v in kept.items() for e in v
              if e["kind"] == "choice" and not e["resolved_inline"]]

out = {"institution": "UTS", "course_code": "C09066v6",
       "course_name": "Bachelor of Engineering (Honours)",
       "total_credit_points": 198,
       "source": "handbookpre2025.uts.edu.au 2024 edition (course page PDF export)",
       "source_url": "https://handbookpre2025.uts.edu.au/2024/courses/c09066.html",
       "structure": {"core": 48, "major": 120,
                     "electives_or_specialist_stream": 24, "professional_practice": 6},
       "major_map_to_usyd": MAJOR_MAP,
       "unmatched_usyd_degrees": UNMATCHED_USYD,
       "all_sequence_names": sorted(sequences),
       "session_position_map": SESSION_POSITION,
       "sessions_seen": sorted(sessions_seen),
       "sessions_unmapped": sorted(sessions_seen - set(SESSION_POSITION)),
       "sequences": kept,
       "subjects_to_fetch": subjects,
       "blocks_to_fetch": blocks,
       "unresolved_choice_groups": unresolved}
json.dump(out, open(OUT, "w"), indent=1)

print(f"credit-point column: main at {MAIN_COL}, nested below {THRESHOLD}")
print(f"sessions found: {sorted(sessions_seen)}")
unmapped = sorted(sessions_seen - set(SESSION_POSITION))
if unmapped:
    print(f"  [WARN] no position mapped for: {unmapped}  <- add them to SESSION_POSITION")
print(f"{len(sequences)} sequences in the PDF, {len(kept)} in scope")
if missing:
    print(f"  [WARN] in-scope majors not found: {sorted(missing)}")
print(f"\n{'major':46}{'fixed':>7}{'choice':>8}{'options':>9}{'unresolved':>12}")
for k in sorted(kept):
    v = kept[k]
    f = sum(1 for e in v if e["kind"] == "fixed")
    c = sum(1 for e in v if e["kind"] == "choice")
    o = sum(len(e["options"]) for e in v if e["kind"] == "choice")
    u = sum(1 for e in v if e["kind"] == "choice" and not e["resolved_inline"])
    print(f"{k[:44]:46}{f:>7}{c:>8}{o:>9}{u:>12}")
print(f"\n{len(subjects)} subject pages to fetch, {len(blocks)} block pages, "
      f"{len(unresolved)} unresolved choice groups")
print(f"wrote {OUT}")
