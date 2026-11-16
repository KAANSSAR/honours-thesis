"""
Re-parse the cached UTS pages. No network access, no refetching.

Fixes the requisite bug: UTS labels prerequisites "Requisite(s):", not
"Prerequisite", so the first pass captured anti-requisites and nothing else. The
negative lookbehind below keeps "Anti-requisite(s):" from matching as a prerequisite.

    python3 reparse_uts.py

Reads uts_cache/ and uts_sequences.json, rewrites uts_c09066_data.json.
"""
import json, os, re, sys, glob
from bs4 import BeautifulSoup

CACHE = "uts_cache"
SEQ_IN = "uts_sequences.json"
OUT = "uts_c09066_data.json"
BASE = "https://handbookpre2025.uts.edu.au/2024"

# "Requisite(s):" is the prerequisite label. The lookbehind stops
# "Anti-requisite(s):" being swallowed by it.
REQ_LABELS = (
    ("prerequisite_codes", r"(?<!Anti-)(?<!anti-)\bRequisite\(?s?\)?\s*:"),
    ("corequisite_codes", r"\bCorequisite\(?s?\)?\s*:"),
    ("antirequisite_codes", r"\bAnti-requisite\(?s?\)?\s*:"),
)
STOP = (r"(?=(?:Anti-)?Requisite\(?s?\)?\s*:|Corequisite|Requisite elaboration|"
        r"Recommended studies|Note:|Access conditions|Description|Subject objectives|"
        r"Teaching and|Content|Assessment|Fee|Availability|Credit points|$)")


def flat(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def cached(kind, code):
    """Find the cached file for a subject or directory page, whatever the path shape."""
    pats = [f"*subjects_{code}_html", f"*subjects_details_{code}_html",
            f"*directory_{code.lower()}_html"]
    for p in pats:
        hits = glob.glob(os.path.join(CACHE, p + ".html")) or glob.glob(os.path.join(CACHE, p))
        if hits:
            return open(hits[0], encoding="utf-8", errors="ignore").read()
    hits = [f for f in os.listdir(CACHE) if code.lower() in f.lower()]
    if hits:
        return open(os.path.join(CACHE, hits[0]), encoding="utf-8", errors="ignore").read()
    return None


def parse_subject(code, html):
    body = flat(html)
    rec = {"code": code, "name": "", "credit_points": None, "sessions_offered": [],
           "prerequisite_codes": [], "prerequisite_groups": [], "corequisite_codes": [],
           "antirequisite_codes": [], "credit_point_requirement": None,
           "requirements_raw": "", "institution": "UTS",
           "source_url": f"{BASE}/subjects/{code}.html"}

    nm = re.search(rf"{code}\s+([A-Z][^|]{{2,90}}?)(?:\s+\d+cp|\s+Warning|\s+Requisite|"
                   rf"\s+Anti-requisite|\s+Undergraduate|\s+Postgraduate|$)", body)
    if nm:
        rec["name"] = nm.group(1).strip()
    cpm = re.search(r"\b(\d+)\s*cp\b", body)
    if cpm:
        rec["credit_points"] = int(cpm.group(1))
    rec["sessions_offered"] = sorted({f"{m.group(1)} session" for m in
                                      re.finditer(r"\b(Autumn|Spring|Summer)\s+session", body)})

    for field, label in REQ_LABELS:
        mm = re.search(label + r"\s*(.+?)" + STOP, body, re.IGNORECASE)
        if not mm:
            continue
        clause = mm.group(1).strip()[:1500]
        if not clause or re.match(r"^(there are no|none)\b", clause, re.IGNORECASE):
            continue
        name = {"prerequisite_codes": "Requisite", "corequisite_codes": "Corequisite",
                "antirequisite_codes": "Anti-requisite"}[field]
        rec["requirements_raw"] += f"{name}: {clause}\n"
        rec[field] = sorted(set(re.findall(r"\b(\d{5})\b", clause)))
        if field == "prerequisite_codes":
            groups = []
            for part in re.split(r"\bAND\b", clause, flags=re.IGNORECASE):
                g = sorted(set(re.findall(r"\b(\d{5})\b", part)))
                if g:
                    groups.append(g)
            rec["prerequisite_groups"] = groups
            cpr = re.search(r"(\d+)\s*cp\s+(?:from|of)\s+([^.;]{0,90})", clause, re.IGNORECASE)
            if cpr:
                rec["credit_point_requirement"] = {"min_credit_points": int(cpr.group(1)),
                                                   "scope": cpr.group(2).strip(),
                                                   "raw": clause[:300]}
    return rec


def main():
    if not os.path.isdir(CACHE):
        sys.exit(f"{CACHE}/ not found. Run fetch_uts_pages.py first.")
    seq = json.load(open(SEQ_IN))
    data = json.load(open(OUT)) if os.path.exists(OUT) else dict(seq)

    units, missing = {}, []
    codes = sorted(set(data.get("units", {})) | set(seq["subjects_to_fetch"]))
    for code in codes:
        html = cached("subject", code)
        if not html:
            missing.append(code)
            continue
        units[code] = parse_subject(code, html)
    data["units"] = units

    need = set(seq["subjects_to_fetch"])
    in_scope = {c: u for c, u in units.items() if c in need}
    with_pre = [c for c, u in in_scope.items() if u["prerequisite_codes"]]
    with_co = [c for c, u in in_scope.items() if u["corequisite_codes"]]
    with_anti = [c for c, u in in_scope.items() if u["antirequisite_codes"]]
    with_cpr = [c for c, u in in_scope.items() if u["credit_point_requirement"]]
    no_sess = sorted(c for c, u in in_scope.items() if not u["sessions_offered"])
    no_any = sorted(c for c, u in in_scope.items() if not u["requirements_raw"])

    data["data_gaps"] = {"in_scope_without_sessions": no_sess,
                         "in_scope_without_any_requisite_text": no_any,
                         "subjects_not_in_cache": missing,
                         "unresolved_choice_groups": seq["unresolved_choice_groups"]}
    json.dump(data, open(OUT, "w"), indent=1)

    print(f"re-parsed {len(units)} cached subjects ({len(in_scope)} of the 108 in scope)")
    if missing:
        print(f"  not in cache: {len(missing)} -> {missing[:10]}")
    print(f"\nin-scope requisite coverage:")
    print(f"  with prerequisites:        {len(with_pre):>3} / {len(in_scope)}")
    print(f"  with corequisites:         {len(with_co):>3}")
    print(f"  with anti-requisites:      {len(with_anti):>3}")
    print(f"  with credit-point rules:   {len(with_cpr):>3}")
    print(f"  no requisite text at all:  {len(no_any):>3}")
    print(f"  no sessions recorded:      {len(no_sess):>3}  {no_sess}")
    print(f"\nwrote {OUT}")
    print("\nSanity check: 48024 Programming 2 should list 48023 Programming Fundamentals,")
    print("and 33230 Mathematics 2 should list 33130 Mathematics 1.")
    for c in ("48024", "33230", "48331"):
        u = units.get(c)
        if u:
            print(f"  {c} {u['name'][:34]:36} pre={u['prerequisite_codes']}")


if __name__ == "__main__":
    main()
