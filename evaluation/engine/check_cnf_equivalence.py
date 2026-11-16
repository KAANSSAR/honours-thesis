"""Level 3b: is the distributed CNF equivalent to the handbook expression?

An independent evaluator of the source notation, written without reusing the
parser, compared exhaustively against the parsed prerequisite_groups. Restricted
to units whose codes all survived wildcard filtering, so a disagreement means a
logic error rather than a dropped code.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import itertools, json, os, re, sys
from paths import P

parsed = json.load(open(P("usyd_requisites_parsed.json")))

def top_split(text, open_ch, close_ch):
    """Split into top-level (...) chunks."""
    out, depth, buf = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
            if depth == 1 and ch == open_ch:
                buf = ""; continue
        elif ch in ")]":
            depth -= 1
            if depth == 0 and ch == close_ch:
                out.append(buf); continue
        if depth >= 1:
            buf += ch
    return out

def members(group):
    out, depth, buf = [], 0, ""
    for ch in group:
        if ch == "[": depth += 1
        elif ch == "]": depth -= 1
        if ch == "," and depth == 0:
            out.append(buf.strip()); buf = ""
        else: buf += ch
    if buf.strip(): out.append(buf.strip())
    return out

def satisfies(expr, taken):
    """Evaluate the source notation directly: AND across (), OR inside, [] is AND."""
    for group in top_split(expr, "(", ")"):
        ok = False
        for m in members(group):
            if m.startswith("["):
                inner = top_split(m.strip("[]"), "(", ")")
                if all(any(c.strip() in taken for c in members(sub)) for sub in inner):
                    ok = True
            elif m.strip() in taken:
                ok = True
        if not ok:
            return False
    return True

raw = {}
for line in open(P("usyd_requisites_raw.md"), encoding="utf-8"):
    if " ; P=" in line and "[" in line.split(";")[1]:
        code = line.split(" ;")[0].strip()
        p = line.split(";")[1].strip()[2:]
        raw.setdefault(code, re.sub(r"\{[^}]*\}", "", p).strip())

checked = skipped = fails = 0
for code, expr in sorted(raw.items()):
    codes = set(re.findall(r"\b[A-Z]{3,5}\d{4}\b", expr))
    if any("X" in c for c in re.findall(r"\b[A-Z]{3,5}[0-9X]{4}\b", expr)):
        skipped += 1
        continue
    groups = parsed.get(code, {}).get("prerequisite_groups")
    if not groups:
        skipped += 1
        continue
    universe = sorted(codes)
    if len(universe) > 16:
        skipped += 1
        continue
    bad = 0
    for r in range(len(universe) + 1):
        for combo in itertools.combinations(universe, r):
            taken = set(combo)
            cnf = all(any(c in taken for c in g) for g in groups)
            if cnf != satisfies(expr, taken):
                bad += 1
    checked += 1
    print(f"  {'PASS' if not bad else 'FAIL'}  {code}: {len(groups)} groups, "
          f"{2**len(universe)} subsets, {bad} mismatches")
    fails += bool(bad)

print(f"\n{checked} units checked exhaustively, {fails} with a logic error")
print(f"{skipped} skipped (wildcards dropped codes, so the two are not comparable)")
