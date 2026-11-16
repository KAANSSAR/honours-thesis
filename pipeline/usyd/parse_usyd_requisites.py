"""
Turn the USYD handbook requisite extraction into the dataset's own format.

Input is usyd_requisites_raw.md, one line per unit:

    CODE ; P=(A,B)(C,D) ; C=... ; N=...

Output is usyd_requisites_parsed.json, keyed by unit code, carrying
`prerequisite_groups`, `prerequisite_requirements`, `corequisite_codes`,
`prohibition_codes` and `credit_point_requirements` in the shapes the engine
already reads.

Three things in the handbook do not fit the flat group model and are handled
explicitly rather than dropped.

Wildcards. USYD writes MATH1X61 for the mainstream and advanced versions of one
unit. X is always 0 or 9 (MATH1061 and MATH1961), so a wildcard expands to both,
filtered against the unit data so a code that does not exist is not invented.
A level wildcard such as MEDS2XXX means "any unit in that subject area at that
level" and cannot expand to a code list at all, so it becomes an `other`
alternative carrying its own text.

Conjunctive alternatives. A group like
    (MATH1X61, MATH1971, [(MATH1X21,MATH1931)(MATH1X02)])
has an alternative that is itself a conjunction. `prerequisite_groups` is a flat
disjunction and cannot say that. The group is therefore written twice: the plain
codes go into `prerequisite_groups` so the engine keeps checking what it can,
and the full structure including the conjunctive branch goes into
`prerequisite_requirements` as typed alternatives, with the conjunction recorded
as an `other` alternative. That matches how the engine already treats a UTS
sub-major standing as an alternative to a named subject: recorded, and not
pretended to be checkable.

Non-code requirements. Braces hold things like {36cp of 3000-level} or
{WAM>=70}. Credit-point totals are parsed into `credit_point_requirements`,
which the engine already enforces. A WAM or a permission requirement is recorded
on `unverifiable_requirements` and enforced by nothing, because the dataset
holds no marks.

Usage:
    python3 parse_usyd_requisites.py [--raw usyd_requisites_raw.md] [--out ...]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import argparse
import json
import re
from collections import OrderedDict

RAW_DEFAULT = P("usyd_requisites_raw.md")
OUT_DEFAULT = P("usyd_requisites_parsed.json")

LINE = re.compile(r"^([A-Z]{3,5}\d{4})\s*;\s*P=(.*?)\s*;\s*C=(.*?)\s*;\s*N=(.*)$")
BRACE = re.compile(r"\{([^}]*)\}")
LEVEL_WILDCARD = re.compile(r"^[A-Z]{3,5}\d(?:XXX|XX)$")

# A code is a letter prefix then four characters of the numeric part. The X that
# marks a wildcard only ever appears in the numeric part: MTRX1701 is a real
# unit whose PREFIX contains an X, and treating that as a wildcard produced
# MTR01701. The prefix is therefore matched separately and never substituted.
SPLIT_CODE = re.compile(r"^([A-Z]{3,5}?)([0-9X]{4})$")

# "36cp of 3000-level", "12cp of 1000-level MATH units", "84cp completed",
# "144cp prior study", "72cp of Civil Stream Core units", "30cp of 2000-level"
CP_RULE = re.compile(r"(\d+)\s*cp\b(.*)", re.I)
LEVEL_IN_TEXT = re.compile(r"(\d)000[-\s]?level", re.I)
PREFIX_IN_TEXT = re.compile(r"\b([A-Z]{3,5})\s+units\b")


def expand(code, known=None):
    """
    A printed code to the real codes it stands for, plus a flag.

    A wildcard digit is expanded over every digit and then filtered against the
    codes the dataset actually holds, rather than assuming which two streams the
    X covers. The assumption that X is always 0 or 9 is wrong: INFO1X10 is
    INFO1110 and INFO1910, and expanding to INFO1010 invents a unit that does
    not exist. Filtering is what catches that, so a wildcard with no known
    expansion is reported rather than guessed at.
    """
    if LEVEL_WILDCARD.match(code):
        return [], "level_wildcard"
    match = SPLIT_CODE.match(code)
    if not match:
        return [code], "code"
    prefix, digits = match.groups()
    if "X" not in digits:
        return [code], "code"

    out = []
    for digit in "0123456789":
        candidate = prefix + digits.replace("X", digit, 1)
        if known is None or candidate in known:
            out.append(candidate)
    if not out:
        return [], "unresolved_wildcard"
    return out, "expanded"


def split_groups(text):
    """
    "(A,B)(C,D)" -> [["A","B"], ["C","D"]], keeping bracketed conjunctions whole.

    Parenthesis depth is tracked so that a nested "[(X)(Y)]" is returned as one
    opaque member rather than being split at its inner brackets.
    """
    groups, depth, buf = [], 0, ""
    for ch in text:
        if ch == "(" and depth == 0:
            depth, buf = 1, ""
            continue
        if depth:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    groups.append(buf)
                    continue
            buf += ch
    return [split_members(g) for g in groups if g.strip()]


def split_members(group):
    """Split a group on commas that are not inside square brackets."""
    out, depth, buf = [], 0, ""
    for ch in group:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def parse_credit_rule(text):
    """{36cp of 3000-level or higher Engineering units} -> a dict, or None."""
    match = CP_RULE.search(text)
    if not match:
        return None
    rule = {"min_credit_points": int(match.group(1)), "source_text": text.strip()}
    rest = match.group(2)
    level = LEVEL_IN_TEXT.search(rest)
    if level:
        rule["min_level"] = int(level.group(1)) * 1000
    prefix = PREFIX_IN_TEXT.search(rest)
    if prefix and prefix.group(1) not in ("WAM",):
        rule["prefix"] = prefix.group(1)
    return rule


def known_codes():
    """Every unit code the dataset holds, used to filter wildcard expansions."""
    import api
    engine = api.Engine()
    codes = set(engine.data.get("UTS", {}).get("units", {}))
    for per_degree in engine.data.get("USYD", {}).values():
        codes |= set(per_degree.get("units", {}))
    return codes


def parse_prereq(text, known=None):
    """
    Return (groups, requirements, credit_rules, unverifiable).

    `groups` is the flat form the engine reads today. `requirements` is the typed
    form that keeps what the flat form cannot express.
    """
    annotations = BRACE.findall(text)
    body = BRACE.sub("", text).strip()

    credit_rules, unverifiable = [], []
    for note in annotations:
        rule = parse_credit_rule(note)
        if rule:
            credit_rules.append(rule)
        else:
            unverifiable.append(note.strip())

    if not body or body == "none":
        return [], [], credit_rules, unverifiable

    groups, requirements = [], []
    for members in split_groups(body):
        plain, conjunctions, typed, opaque = [], [], [], False
        for member in members:
            if member.startswith("["):
                # A conjunction standing as one alternative: "A or (B and C)".
                branch = [sorted({c for m in sub for c in expand(m, known)[0]})
                          for sub in split_groups(member.strip("[]"))]
                conjunctions.append(branch)
                typed.append({"kind": "other", "text": member,
                              "mentions": sorted({c for sub in branch for c in sub})})
                continue
            codes, how = expand(member, known)
            if how in ("level_wildcard", "unresolved_wildcard"):
                opaque = True
                typed.append({"kind": "other", "text": member, "reason": how})
                continue
            for code in codes:
                plain.append(code)
                typed.append({"kind": "subject", "code": code,
                              "printed_as": member if how == "expanded" else code})

        if typed:
            requirements.append(typed)

        # `prerequisite_groups` is the field the engine treats as compulsory, so
        # only a group it can actually decide belongs there.
        if opaque:
            # One alternative cannot be checked, so the remaining codes are not
            # compulsory: a student may be satisfying the group by the route the
            # dataset cannot see. Recording the rest as a hard group would reject
            # a correct plan, which is the fault Section 4.2.2 reports.
            continue
        if not conjunctions:
            if plain:
                groups.append(plain)
            continue

        # Distribute, so that the conjunctive branch survives in a form the flat
        # field can express: "T or (A and B)" becomes "(T or A) and (T or B)".
        # Dropping the branch instead makes T compulsory, which is exactly how
        # MECH3261 came to reject seven correct plans.
        if any(not sub for branch in conjunctions for sub in branch):
            continue              # a branch with an unknown code cannot distribute
        import itertools
        for selection in itertools.product(*[b for b in conjunctions]):
            merged = list(plain)
            for sub in selection:
                merged.extend(sub)
            if merged:
                groups.append(sorted(set(merged)))

    return groups, requirements, credit_rules, unverifiable


def parse_codes(text, known=None):
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part or part == "none":
            continue
        for code in expand(part, known)[0]:
            out.append(code)
    return out


def parse_file(path, known=None):
    units, source = OrderedDict(), None
    skipped = []
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("## "):
            source = line[3:].strip()
            continue
        if " ; P=" not in line:
            continue
        match = LINE.match(line)
        if not match:
            skipped.append(line)
            continue
        code, p, c, n = match.groups()
        groups, requirements, credit_rules, unverifiable = parse_prereq(p, known)
        record = {"source_table": source}
        if groups:
            record["prerequisite_groups"] = groups
        if requirements:
            record["prerequisite_requirements"] = requirements
        if credit_rules:
            # The engine reads the singular field (rules_usyd.py) and the
            # independent checker reads either, so both are written. The singular
            # one carries the least demanding rule, matching how the checker
            # picks when a unit has several: requiring the strictest would reject
            # a student who satisfied the published alternative.
            record["credit_point_requirements"] = credit_rules
            record["credit_point_requirement"] = min(
                credit_rules, key=lambda r: r["min_credit_points"])
        if unverifiable:
            record["unverifiable_requirements"] = unverifiable
        coreqs, prohibitions = parse_codes(c, known), parse_codes(n, known)
        if coreqs:
            record["corequisite_codes"] = coreqs
        if prohibitions:
            record["prohibition_codes"] = prohibitions
        # A unit listed on two tables: keep the first, note the duplicate only
        # if its code structure actually differs.
        if code in units:
            before = {k: v for k, v in units[code].items() if k != "source_table"}
            after = {k: v for k, v in record.items() if k != "source_table"}
            if before != after:
                units[code].setdefault("also_listed_on", []).append(
                    {"table": source, "record": after})
            continue
        units[code] = record
    return units, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=RAW_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    known = known_codes()
    units, skipped = parse_file(args.raw, known)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(units, fh, indent=1, sort_keys=True)

    with_groups = sum(1 for u in units.values() if u.get("prerequisite_groups"))
    with_typed_only = sum(1 for u in units.values()
                          if u.get("prerequisite_requirements")
                          and not u.get("prerequisite_groups"))
    with_cp = sum(1 for u in units.values() if u.get("credit_point_requirements"))
    conflicts = sum(1 for u in units.values() if u.get("also_listed_on"))

    unresolved = sorted({alt["text"]
                         for u in units.values()
                         for grp in u.get("prerequisite_requirements", [])
                         for alt in grp
                         if alt.get("reason") == "unresolved_wildcard"})
    print(f"parsed {len(units)} units from {args.raw}")
    print(f"  known codes used to filter wildcards: {len(known)}")
    print(f"  with a code prerequisite        : {with_groups}")
    print(f"  with only an unverifiable one   : {with_typed_only}")
    print(f"  with a credit-point requirement : {with_cp}")
    print(f"  listed twice with a real difference: {conflicts}")
    if unresolved:
        print(f"  wildcards with no known expansion : {len(unresolved)} {unresolved}")
    if skipped:
        print(f"  lines that did not parse        : {len(skipped)}")
        for line in skipped[:10]:
            print(f"      {line[:100]}")
    print(f"wrote {args.out}")

    if args.verbose:
        for code in sorted(units)[:5]:
            print(f"\n{code}: {json.dumps(units[code], indent=2)}")


if __name__ == "__main__":
    main()
