"""
Re-parse the cached UTS pages. No network access, no refetching.

Supersedes reparse_uts.py. Run it the same way:

    python3 reparse_uts2.py

Reads uts_cache/, uts_sequences.json and, if present, sequence_overrides.json, then
rewrites uts_c09066_data.json. No network access and no PDF needed.

Five faults in the previous pass, all found by auditing its own output:

1. "Credit points" sat in the clause-terminator list and the match was
   case-insensitive, so a requisite reading "42 credit points of completed study
   in Bachelor's Degree owned by FEIT" was cut off after "42". That truncated
   41200, 41201, 41202 and 48850 to nothing, lost 48230 as a prerequisite of
   41200, and is the whole reason the previous run reported zero credit-point
   rules. The terminator list now holds only labels that genuinely follow the
   requisite field, and matching is case-sensitive.

2. UTS marks a corequisite by suffixing the code with a lowercase "c" inside the
   ordinary Requisite clause ("33230c Mathematics 2"). There is no separate
   Corequisite label anywhere on these pages, which is why the previous run found
   none. The old \\b(\\d{5})\\b could not match "33230c" at all, so 20 codes
   across 10 subjects were dropped rather than merely misfiled.

3. Prerequisite groups were produced by splitting the clause on "AND", which is
   wrong once parentheses nest: 20 of the 108 in-scope clauses nest two or three
   deep. The clause is now tokenised and parsed into an expression tree, then
   converted to conjunctive normal form, so the groups are a correct
   AND-of-ORs rather than an approximation.

4. Trailing prose ran into the clause ("There are course requisites for this
   subject. See", "Undergraduate and Postgraduate"). It injected no spurious
   codes, but it did distort the groups. It is now trimmed.

5. Nine in-scope subjects have no availability printed in the handbook at all,
   confirmed by hand against 41200. That is a gap in the source, not a parse
   failure, so it cannot be fixed by scraping harder. Their sessions are instead
   inferred from where the suggested sequences place them and flagged
   sessions_inferred, so the inference stays visible rather than passing as
   scraped fact.

6. The sequence parser fell back to 6cp whenever a course-page PDF line carried no
   credit-point figure. 41028 Engineering Work Experience is a 0cp subject, so its
   line has no figure and it was recorded as 6cp in all seven majors. That is the
   entire reason every major summed to 204cp against the handbook's 198, and it
   inflated every Year 3 Spring to 30cp. The subject page is now authoritative for
   an entry's credit points, and every correction is recorded in
   data_gaps.sequence_credit_points_corrected rather than applied silently.

7. The sequence parser recognised only Autumn, Spring and Summer session headings.
   C09066 places 48850 Environmental Planning and Law in a Year 3 December session,
   so that heading did not register, the preceding Spring heading stayed in force,
   and 48850 was recorded in Year 3 Spring. That left Civil and Environmental at
   30cp in Y3 Spring against 21cp in Y4 Autumn while every other major sat at 24 or
   27. It also produced a wrong inferred availability, because 48850 is one of the
   nine subjects with no published availability and fault 5's inference took its
   session from this very placement. parse_uts_sequences.py now accepts any
   "<Name> session" heading; sequence_overrides.json corrects the already-parsed
   data without needing the PDF, and reports itself redundant once the parser is
   re-run against a regenerated one.

8. Credit-point thresholds were lifted out of the clause into a separate list,
   which turned disjunctions into conjunctions. 41029's requisite is "(48260 OR
   16912 OR 41202) AND (41048 OR 144cp in C09066 OR ...)", and lifting the
   thresholds out left 41048 as mandatory. 41048 is not part of C09066 at all, so
   all seven majors reported a violation that does not exist. 42001 failed the same
   way and made a published choice group look like a dead end. Validating the seven
   default plans produced 32 findings, of which 25 were this single fault.
   Requirements are now typed: prerequisite_requirements is an AND of ORs whose
   alternatives may be a subject, a credit-point threshold, or uncheckable text.
   Corequisites get the same treatment, so 41066's seven "c"-marked alternatives
   read as one choice rather than seven obligations. The handbook's own "These
   requisites may not apply to students in certain courses" note is recorded too,
   which is what makes 31269 explicable: it asks for 18cp of completed study while
   the Software sequence places it in Year 1 Autumn.

9. Availability was extracted by scanning the whole page for "<Season> session".
   UTS labels the field "Typical availability" and words it in two ways: singular
   ("Spring session, City campus") and conjoined plural ("Autumn and Spring
   sessions, City campus"). On the plural form the pattern matched "Spring
   sessions" but not "Autumn and", so Autumn was silently dropped: 41057 and 41059
   both run in Autumn and Spring and were recorded as Spring only. Adding a word
   boundary after "session" makes the plural form match nothing at all, which is
   how two diagnostics came to disagree about the same file. Availability is now
   read from the labelled region and every season named in it is taken, with the
   region kept verbatim in `availability_raw` so the reading can be checked.

   Note for the write-up: the field is "Typical availability", not a guarantee.
   Treating it as a hard constraint is a modelling choice, not something the
   handbook asserts.

Requisites that are course-level rather than subject-level (a credit-point
threshold, a named course, a "Spks Between" clause) carry no subject code. They
are recorded in credit_point_requirements and unmodelled_requisite_text and left
out of prerequisite_groups, so the planner never silently treats one as met.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from paths import P

import json, os, re, sys, glob
from collections import Counter
from bs4 import BeautifulSoup

CACHE = P("uts_cache")
SEQ_IN = P("uts_sequences.json")
SEQ_OVERRIDES = P("sequence_overrides.json")
OUT = P("uts_c09066_data.json")
BASE = "https://handbookpre2025.uts.edu.au/2024"

# The prerequisite label is "Requisite(s):". The lookbehind keeps
# "Anti-requisite(s):" from matching as one.
REQ_LABELS = (
    ("prerequisite", r"(?<!Anti-)(?<!anti-)\bRequisite\(?s?\)?\s*:"),
    ("antirequisite", r"\bAnti-requisite\(?s?\)?\s*:"),
)

# Only labels that actually follow the requisite field on a UTS subject page.
# "Credit points" and "Availability" are deliberately absent: both occur inside
# requisite prose, and matching them there is fault 1 above. Case-sensitive, so
# "credit points of completed study" cannot terminate a clause.
STOP = (r"(?=(?:[Aa]nti-)?[Rr]equisite\(?s?\)?\s*:|Requisite elaboration|Recommended studies|"
        r"Subject objectives|Subject description|Description|Fields of pract|"
        r"Subject learning|Content \(topics\)|Teaching and learning|"
        r"Assessment|Minimum requirements|Required texts|Fee|$)")
# The lowercase alternatives in the first branch are not cosmetic. The handbook
# writes "Anti-requisite(s):" with a lowercase r, and this lookahead is matched
# case-sensitively, so a pattern reading "(?:Anti-)?Requisite" never matched the
# anti-requisite heading at all. A prerequisite clause therefore ran straight
# through it wherever no other terminator intervened, and 41030 Engineering
# Capstone came out listing its own anti-requisites as prerequisites. Matching the
# whole clause case-insensitively is not the fix, because that is what made
# "Credit points" fire inside "42 credit points of completed study"; the fix is to
# spell both cases out.
#
# "Description" as a bare word matters more than it looks. A clause reading
#   "Anti-requisite: 48342 Structural Behaviour and Design Undergraduate Description
#    This subject covers ... 48353 ... 48366 ..."
# ran past the label into the description and took every five-digit number in the
# prose as a requisite. 225 of 327 clauses run into description text; 11 of them
# contain a code there, and those 11 produced requisites that do not exist: 41029
# and 41030, both compulsory in every major, came out mutually exclusive, and
# 48349 came out excluding two core Civil subjects. "Fields of pract" is spelled
# that way on purpose, because the handbook itself has both "Fields of practice"
# and the typo "Fields of practive".

# Prose that runs on past the end of a clause.
TRAIL = re.compile(
    r"\s*(?:There are course requisites for this subject\.?.*"
    r"|See\s*$"
    r"|(?:Engineering Practice\s+)?(?:Undergraduate|Postgraduate)(?:\s+and\s+(?:Undergraduate|Postgraduate))?\s*$"
    r")", re.IGNORECASE)

# Availability lives in a labelled region, so it is read from that region rather
# than by scanning the whole page for a phrase. Scanning was wrong twice over.
# A page reading "Typical availability Autumn and Spring sessions, City campus"
# conjoins the seasons and pluralises "sessions", so the pattern
# r"(Autumn|Spring|Summer)\s+session" matched "Spring sessions" but not
# "Autumn and", silently dropping Autumn: 41057 and 41059 both run in Autumn and
# Spring and were recorded as Spring only. Adding \b after "session" does not fix
# it, it just makes the plural form match nothing at all. Scanning the page also
# risks the opposite error, picking up a season named in prose.
# Read in three tiers, most precise first, and record which tier answered. An
# earlier version used one regex that demanded a terminator phrase within 240
# characters of the label; on 52 pages that phrase is not there, the whole match
# failed, and availability came back empty rather than falling back. Losing a
# real value is worse than reading it imprecisely, so each tier now degrades into
# the next and the tier is recorded in sessions_source.
AVAIL_LABEL = re.compile(r"(?:Typical availability|Availability)\s*[:\-]?\s*",
                         re.IGNORECASE)
AVAIL_STOP = re.compile(
    r"Detailed subject description|Access conditions|Subject description|"
    r"Subject objectives|Teaching and learning|Minimum requirements|"
    r"Required texts|Recommended|Assessment|Anti-requisite|Requisite|Fee|"
    r"Credit points|Note:", re.IGNORECASE)
SEASON_WORD = re.compile(r"\b(Autumn|Spring|Summer|Winter|December|January|July)\b")
# "Spring session" and "Autumn and Spring sessions" and "Autumn, Spring and Summer
# sessions" all have to parse. The conjoined plural form is the one that broke the
# original pattern.
SEASON_SPAN = re.compile(
    r"((?:Autumn|Spring|Summer|Winter|December|January|July)"
    r"(?:\s*(?:,|and|&)\s*(?:Autumn|Spring|Summer|Winter|December|January|July))*)"
    r"\s+sessions?\b", re.IGNORECASE)


def read_availability(body):
    """Return (sessions, tier, raw). Tiers run precise -> permissive -> nothing."""
    m = AVAIL_LABEL.search(body)
    if m:
        region = body[m.end():m.end() + 220]
        stop = AVAIL_STOP.search(region)
        if stop:
            region = region[:stop.start()]
        seasons = SEASON_WORD.findall(region)
        if seasons:
            return (sorted({f"{s.title()} session" for s in seasons}),
                    "handbook availability field", region.strip()[:200])

    # The label is missing or named no season. Fall back to the phrase anywhere on
    # the page. This is what the original scan did, so it recovers every value that
    # scan found, but it can pick up a season named in prose, so it is marked.
    hits = []
    for sm in SEASON_SPAN.finditer(body):
        hits.extend(SEASON_WORD.findall(sm.group(1)))
    if hits:
        return (sorted({f"{s.title()} session" for s in hits}),
                "page scan fallback, availability field not found",
                (m and body[m.end():m.end() + 120].strip()[:200]) or "")
    return [], "", ""

CODE = re.compile(r"\b(\d{5})(c?)\b")
CP_RULE = re.compile(r"(\d+)\s*(?:cp|credit points?)\s+(?:of|from)\s+([^.;)]{0,90})", re.IGNORECASE)


def flat(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))


def cached(code):
    """Find the cached file for a subject page, whatever path shape it was saved under."""
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


# ---------------------------------------------------------------- clause parsing

def tokenise(clause):
    """Split a requisite clause into parentheses, AND/OR operators and atoms.

    AND and OR are matched case-sensitively: subject names contain lowercase
    "and" and "or" ("Introduction to Analysis and Multivariable Calculus"), and
    treating those as operators would shatter the clause.
    """
    out, buf = [], ""
    i = 0
    while i < len(clause):
        ch = clause[i]
        if ch in "()":
            if buf.strip():
                out.append(("atom", buf.strip()))
            buf = ""
            out.append((ch, ch))
            i += 1
            continue
        m = re.match(r"\b(AND|OR)\b", clause[i:])
        if m:
            if buf.strip():
                out.append(("atom", buf.strip()))
            buf = ""
            out.append(("op", m.group(1)))
            i += m.end()
            continue
        buf += ch
        i += 1
    if buf.strip():
        out.append(("atom", buf.strip()))
    return out


def parse_expr(tokens, pos=0):
    """expr := term (OR term)* ; term := factor (AND factor)* ; AND binds tighter.

    Returns (node, next_pos). A node is ("lit", text), ("and", [...]) or ("or", [...]).
    Unbalanced parentheses are tolerated: a missing ")" just ends the group.
    """
    def factor(p):
        if p < len(tokens) and tokens[p][0] == "(":
            node, p = parse_expr(tokens, p + 1)
            if p < len(tokens) and tokens[p][0] == ")":
                p += 1
            return node, p
        if p < len(tokens) and tokens[p][0] == "atom":
            return ("lit", tokens[p][1]), p + 1
        return None, p + 1

    def term(p):
        parts = []
        node, p = factor(p)
        if node:
            parts.append(node)
        while p < len(tokens) and tokens[p] == ("op", "AND"):
            node, p = factor(p + 1)
            if node:
                parts.append(node)
        if not parts:
            return None, p
        return (parts[0] if len(parts) == 1 else ("and", parts)), p

    parts = []
    node, pos = term(pos)
    if node:
        parts.append(node)
    while pos < len(tokens) and tokens[pos] == ("op", "OR"):
        node, pos = term(pos + 1)
        if node:
            parts.append(node)
    if not parts:
        return None, pos
    return (parts[0] if len(parts) == 1 else ("or", parts)), pos


def literal(text):
    """Classify one atom: a subject code, a corequisite-marked code, or neither."""
    m = CODE.search(text)
    if m:
        return {"kind": "coreq" if m.group(2) else "subject", "code": m.group(1),
                "text": text[:120]}
    cp = CP_RULE.search(text)
    if cp:
        return {"kind": "credit_points", "min_credit_points": int(cp.group(1)),
                "scope": cp.group(2).strip(), "text": text[:160]}
    return {"kind": "other", "text": text[:160]}


def annotate(node):
    """Replace each ("lit", text) with its classified literal, in place of the text."""
    if node is None:
        return None
    if node[0] == "lit":
        return ("lit", literal(node[1]))
    return (node[0], [annotate(c) for c in node[1]])


def cnf(node):
    """Convert an AND/OR tree to conjunctive normal form: a list of OR-clauses.

    OR distributes over AND, so (a AND b) OR c becomes (a OR c) AND (b OR c).
    Clauses here hold at most a handful of literals, so the distribution cannot
    blow up in practice.
    """
    if node is None:
        return []
    if node[0] == "lit":
        return [[node[1]]]
    if node[0] == "and":
        out = []
        for ch in node[1]:
            out.extend(cnf(ch))
        return out
    out = [[]]
    for ch in node[1]:
        out = [a + b for a in out for b in cnf(ch)]
    return out


def parse_clause(clause):
    """Parse one requisite clause into typed requirements plus flat summary views.

    The important output is `requirements`: a list of requirements, all of which
    must hold, where each is a list of alternatives, any one of which satisfies it.
    An alternative may be a subject, a credit-point threshold, or text the planner
    cannot check.

    Keeping credit-point thresholds inside the alternatives is the whole point. An
    earlier version pulled them out into a separate list, which silently turned
    disjunctions into conjunctions and produced wrong answers on real clauses.
    41029's requisite is:

        (48260 OR 16912 OR 41202) AND (41048 OR 144cp in C09066 OR 144cp in C09067 ...)

    Lifting the thresholds out left `[['41048']]` as a hard requirement, and 41048
    is not in C09066 at all, so every one of the seven majors reported a violation
    that does not exist: a C09066 student satisfies that half on credit points.
    42001's requisite failed the same way and made a choice group look like a dead
    end. Of 32 validation findings across the seven plans, 25 were this one bug.

    The flat views (`pre`, `groups`, `coreq`) are kept for readers that want codes
    only. They are lossy by construction and `requirements` is authoritative.
    """
    tree = annotate(parse_expr(tokenise(clause))[0])

    requirements, coreq_groups = [], []
    pre, coreq, cps, other, groups = set(), set(), [], [], []
    for group in cnf(tree):
        # A "c"-suffixed code is a corequisite, so a disjunction of them is a
        # separate corequisite requirement rather than part of the prerequisite.
        cs = [l for l in group if l["kind"] == "coreq"]
        rest = [l for l in group if l["kind"] != "coreq"]
        if cs:
            coreq_groups.append([{k: v for k, v in l.items()} for l in cs])
            coreq.update(l["code"] for l in cs)
        if not rest:
            continue
        requirements.append([{k: v for k, v in l.items()} for l in rest])

        codes = sorted({l["code"] for l in rest if l["kind"] == "subject"})
        for l in rest:
            if l["kind"] == "subject":
                pre.add(l["code"])
            elif l["kind"] == "credit_points":
                cps.append({k: v for k, v in l.items() if k != "kind"})
            elif l["text"].strip():
                other.append(l["text"].strip())
        if codes:
            groups.append(codes)

    seen, uniq = set(), []
    for g in groups:
        if tuple(g) not in seen:
            seen.add(tuple(g))
            uniq.append(g)
    return (requirements, coreq_groups, sorted(pre), uniq, sorted(coreq), cps,
            sorted({o for o in other if len(o) > 3}))


# ---------------------------------------------------------------- page parsing

def parse_subject(code, html):
    body = flat(html)
    rec = {"code": code, "name": "", "credit_points": None, "sessions_offered": [],
           "sessions_inferred": False, "sessions_source": "handbook subject page",
           "availability_raw": "",
           "prerequisite_requirements": [], "corequisite_requirements": [],
           "requisite_waived_for_some_courses": False,
           "prerequisite_codes": [], "prerequisite_groups": [], "corequisite_codes": [],
           "antirequisite_codes": [], "credit_point_requirements": [],
           "unmodelled_requisite_text": [], "requirements_raw": "",
           "institution": "UTS", "source_url": f"{BASE}/subjects/{code}.html"}
    # The handbook prints this next to a requisite that the student's own course
    # may exempt them from. 31269 Business Requirements Modelling is the clear
    # case: it asks for 18cp of completed study, yet the Software sequence places
    # it in Year 1 Autumn, where no student can have any. Without reading the
    # note, that reads as an unsatisfiable core subject.
    rec["requisite_waived_for_some_courses"] = bool(
        re.search(r"These requisites may not apply to students in certain courses",
                  body, re.IGNORECASE))

    nm = re.search(rf"{code}\s+([A-Z][^|]{{2,90}}?)(?:\s+\d+\s*cp|\s+Warning|\s+Requisite|"
                   rf"\s+Anti-requisite|\s+Undergraduate|\s+Postgraduate|$)", body)
    if nm:
        rec["name"] = nm.group(1).strip()
    cpm = re.search(r"Credit points?\s*:?\s*(\d+)\s*cp", body, re.IGNORECASE) \
        or re.search(r"\b(\d+)\s*cp\b", body)
    if cpm:
        rec["credit_points"] = int(cpm.group(1))
    sessions, tier, raw = read_availability(body)
    rec["sessions_offered"] = sessions
    rec["availability_raw"] = raw
    if tier:
        rec["sessions_source"] = tier

    for field, label in REQ_LABELS:
        mm = re.search(label + r"\s*(.+?)" + STOP, body)
        if not mm:
            continue
        clause = TRAIL.sub("", mm.group(1).strip())[:2000].strip()
        if not clause or re.match(r"^(there are no|none)\b", clause, re.IGNORECASE):
            continue
        name = "Requisite" if field == "prerequisite" else "Anti-requisite"
        rec["requirements_raw"] += f"{name}: {clause}\n"

        if field == "antirequisite":
            rec["antirequisite_codes"] = sorted({m.group(1) for m in CODE.finditer(clause)})
            continue

        reqs, coreqs, pre, groups, coreq, cps, other = parse_clause(clause)
        rec["prerequisite_requirements"] = reqs
        rec["corequisite_requirements"] = coreqs
        rec["prerequisite_codes"] = pre
        rec["prerequisite_groups"] = groups
        rec["corequisite_codes"] = coreq
        rec["credit_point_requirements"] = cps
        rec["unmodelled_requisite_text"] = other
    return rec


def apply_sequence_overrides(sequences, path=SEQ_OVERRIDES):
    """Apply recorded corrections to the parsed sequences, auditably.

    Same discipline as data_overrides.json on the USYD side: a correction states
    what it changes, why, and the source it was checked against, and the result is
    reported rather than applied quietly. An override whose target is already
    correct counts as "already correct", not as a failure, so re-running the
    sequence parser against a regenerated PDF makes these redundant instead of
    breaking the run.

    Returns (applied, already_correct, not_found).
    """
    if not os.path.exists(path):
        return [], [], []
    spec = json.load(open(path))
    applied, already, missing = [], [], []
    for mv in spec.get("moves", []):
        ents = sequences.get(mv["major"])
        if ents is None:
            missing.append({**mv, "problem": f"major not in sequences: {mv['major']}"})
            continue
        hit = None
        for e in ents:
            if e["kind"] != "fixed" or e["code"] != mv["code"]:
                continue
            if (e["year"], e["session"]) == (mv["to"]["year"], mv["to"]["session"]):
                hit = "already"
                break
            if (e["year"], e["session"]) == (mv["from"]["year"], mv["from"]["session"]):
                e["year"] = mv["to"]["year"]
                e["session"] = mv["to"]["session"]
                e["corrected"] = True
                e["correction_source"] = spec.get("source_checked")
                hit = "applied"
                break
        if hit == "applied":
            applied.append(mv)
        elif hit == "already":
            already.append(mv)
        else:
            missing.append({**mv, "problem": "no entry matched the 'from' position"})
    return applied, already, missing


def reconcile_sequence_credit_points(units, sequences):
    """Make the subject page authoritative for an entry's credit points.

    parse_uts_sequences.py reads credit points off the course-page PDF and falls
    back to 6 when a line carries no figure. 41028 Engineering Work Experience is
    a 0cp subject, so its line has no figure, and the fallback silently gave it
    6cp in all seven majors. That is the whole of the 204cp-against-198cp
    discrepancy, and it also inflated every Year 3 Spring to 30cp.

    The subject page states credit points outright, so it wins. Disagreements are
    recorded rather than quietly corrected.
    """
    fixed = []
    for maj, ents in sequences.items():
        for e in ents:
            if e["kind"] != "fixed":
                continue
            page = units.get(e["code"], {}).get("credit_points")
            if page is None or page == e["credit_points"]:
                continue
            fixed.append({"major": maj, "year": e["year"], "session": e["session"],
                          "code": e["code"], "from_pdf": e["credit_points"],
                          "from_subject_page": page})
            e["credit_points"] = page
    return fixed


def sequence_loads(sequences, expected_total):
    """Per-major total and per-session load, for checking against the handbook."""
    out = {}
    for maj, ents in sequences.items():
        load = {}
        for e in ents:
            load.setdefault(f"Y{e['year']} {e['session']}", 0)
            load[f"Y{e['year']} {e['session']}"] += e.get("credit_points", 0)
        total = sum(load.values())
        out[maj] = {"total_credit_points": total,
                    "matches_handbook_total": total == expected_total,
                    "session_loads": load,
                    "sessions_over_24cp": {k: v for k, v in load.items() if v > 24}}
    return out


def infer_sessions(units, sequences):
    """Fill sessions for subjects the handbook gives no availability for.

    Nine in-scope subjects print no availability at all (verified by hand on
    41200). Where a suggested sequence places such a subject, that placement is
    direct evidence of when it runs, so it is used, and flagged as inferred.
    """
    placed = {}
    for ents in sequences.values():
        for e in ents:
            codes = [e["code"]] if e["kind"] == "fixed" else [o["code"] for o in e["options"]]
            for c in codes:
                placed.setdefault(c, set()).add(f"{e['session']} session")
    n = 0
    for code, u in units.items():
        if u["sessions_offered"] or code not in placed:
            continue
        u["sessions_offered"] = sorted(placed[code])
        u["sessions_inferred"] = True
        u["sessions_source"] = "inferred from suggested sequence placement"
        n += 1
    return n


def main():
    if not os.path.isdir(CACHE):
        sys.exit(f"{CACHE}/ not found. Run fetch_uts_pages.py first.")
    seq = json.load(open(SEQ_IN))
    # Corrections first: sessions are inferred from sequence placement further
    # down, so a placement must be right before anything is derived from it.
    ov_applied, ov_already, ov_missing = apply_sequence_overrides(seq["sequences"])
    data = json.load(open(OUT)) if os.path.exists(OUT) else dict(seq)
    data["sequences"] = seq["sequences"]

    # Keep the previous availability so the change can be reported, not just applied.
    was = {c: (u.get("sessions_offered") or [], u.get("sessions_inferred"))
           for c, u in (data.get("units") or {}).items()}

    units, missing = {}, []
    for code in sorted(set(data.get("units", {})) | set(seq["subjects_to_fetch"])):
        html = cached(code)
        if not html:
            missing.append(code)
            continue
        units[code] = parse_subject(code, html)
    inferred = infer_sessions(units, seq["sequences"])
    cp_fixed = reconcile_sequence_credit_points(units, seq["sequences"])
    loads = sequence_loads(seq["sequences"], data.get("total_credit_points", 198))
    data["units"] = units
    data["sequence_loads"] = loads

    need = set(seq["subjects_to_fetch"])
    ins = {c: u for c, u in units.items() if c in need}
    def count(f):
        return sum(1 for u in ins.values() if f(u))
    no_sess = sorted(c for c, u in ins.items() if not u["sessions_offered"])
    no_any = sorted(c for c, u in ins.items() if not u["requirements_raw"])
    unmod = sorted(c for c, u in ins.items() if u["unmodelled_requisite_text"])

    data["data_gaps"] = {
        "in_scope_without_sessions": no_sess,
        "in_scope_sessions_inferred": sorted(c for c, u in ins.items() if u["sessions_inferred"]),
        "in_scope_without_any_requisite_text": no_any,
        "in_scope_with_unmodelled_requisite_text": unmod,
        "subjects_not_in_cache": missing,
        "sequence_credit_points_corrected": cp_fixed,
        "sequence_overrides_applied": ov_applied,
        "sequence_overrides_already_correct": ov_already,
        "sequence_overrides_not_matched": ov_missing,
        "unresolved_choice_groups": seq["unresolved_choice_groups"]}
    json.dump(data, open(OUT, "w"), indent=1)

    print(f"re-parsed {len(units)} cached subjects "
          f"({len(ins)} of the {len(need)} in scope)")
    if missing:
        print(f"  not in cache: {len(missing)} -> {missing[:10]}")
    print("\nin-scope requisite coverage:")
    print(f"  with prerequisites:        {count(lambda u: u['prerequisite_codes']):>3} / {len(ins)}")
    print(f"  with corequisites:         {count(lambda u: u['corequisite_codes']):>3}"
          f"   (UTS marks these with a 'c' suffix, not a separate label)")
    print(f"  with anti-requisites:      {count(lambda u: u['antirequisite_codes']):>3}")
    print(f"  with credit-point rules:   {count(lambda u: u['credit_point_requirements']):>3}")
    print(f"  no requisite text at all:  {len(no_any):>3}")
    print(f"  sessions inferred:         {inferred:>3}")
    print(f"  still no sessions:         {len(no_sess):>3}  {no_sess}")
    print(f"  course-level text the planner cannot check: {len(unmod)}")
    print(f"  requisite waived for some courses:          "
          f"{count(lambda u: u['requisite_waived_for_some_courses']):>3}"
          f"   (handbook note, not a parse result)")
    mixed = count(lambda u: any(
        any(a['kind'] == 'credit_points' for a in r) and
        any(a['kind'] == 'subject' for a in r)
        for r in u['prerequisite_requirements']))
    print(f"  requirements mixing a subject with a credit-point alternative: {mixed}")

    print("\nRegression checks (each one failed in the previous pass):")
    checks = [
        ("41200", "48230", "clause was truncated at 'Credit points'"),
        ("41056", "33230", "corequisite marked 33230c, code was dropped"),
        ("48521", "33230", "corequisite marked 33230c, code was dropped"),
        ("48024", "48023", "ordinary OR alternatives, should still hold"),
        ("33230", "33130", "ordinary OR alternatives, should still hold"),
    ]
    for code, want, why in checks:
        u = units.get(code)
        if not u:
            print(f"  [SKIP] {code} not in cache")
            continue
        got = want in u["prerequisite_codes"] or want in u["corequisite_codes"]
        print(f"  [{'ok  ' if got else 'FAIL'}] {code} -> {want:5}  {why}")

    # 41029's second requirement is "41048 OR 144cp in C09066 OR ...". If the
    # credit-point alternatives are not inside the requirement, 41048 reads as
    # mandatory and all seven plans report a violation that does not exist.
    u = units.get("41029")
    mixed = [r for r in (u or {}).get("prerequisite_requirements", [])
             if any(a["kind"] == "credit_points" for a in r)
             and any(a["kind"] == "subject" for a in r)]
    print(f"  [{'ok  ' if mixed else 'FAIL'}] 41029: 41048 kept as an alternative to "
          f"the 144cp rule, not a separate requirement")
    u = units.get("31269")
    print(f"  [{'ok  ' if (u or {}).get('requisite_waived_for_some_courses') else 'FAIL'}]"
          f" 31269: waiver note detected (18cp asked for in a Year 1 Autumn subject)")
    u = units.get("41066")
    cg = (u or {}).get("corequisite_requirements") or []
    print(f"  [{'ok  ' if cg and len(cg[0]) > 1 else 'FAIL'}] 41066: seven corequisites "
          f"kept as one OR requirement, not seven mandatory ones")
    for code in ("41057", "41059"):
        got = (units.get(code) or {}).get("sessions_offered") or []
        ok = got == ["Autumn session", "Spring session"]
        print(f"  [{'ok  ' if ok else 'FAIL'}] {code}: \"Autumn and Spring sessions\" "
              f"read as both, not Spring alone -> {got}")

    # Availability was read from the labelled region for the first time, so say what
    # that changed. A conjoined "Autumn and Spring sessions" previously lost Autumn,
    # which understates availability and makes plans look infeasible that are not.
    gained, lost, same = [], [], 0
    for c, u in units.items():
        old = set(was.get(c, ([], None))[0])
        new = set(u["sessions_offered"])
        if u["sessions_inferred"] or c not in was:
            continue
        if new - old:
            gained.append((c, sorted(old), sorted(new)))
        elif old - new:
            lost.append((c, sorted(old), sorted(new)))
        else:
            same += 1
    print(f"\nAvailability, read in three tiers:")
    tiers = Counter(u["sessions_source"] for u in units.values())
    for t, n in tiers.most_common():
        print(f"  {n:>4}  {t or '(none: no availability found at all)'}")
    blank = sorted(c for c, u in units.items() if not u["sessions_offered"])
    print(f"\n  changed since last run: gained {len(gained)}, lost {len(lost)}, "
          f"unchanged {same}")
    for c, o, n in gained[:12]:
        print(f"    gained  {c} {(units[c]['name'] or '')[:32]:34} {o} -> {n}")
    if len(gained) > 12:
        print(f"    ... {len(gained) - 12} more gained")
    for c, o, n in lost[:12]:
        print(f"    [CHECK] {c} {(units[c]['name'] or '')[:32]:34} {o} -> {n}")
    if len(lost) > 12:
        print(f"    ... {len(lost) - 12} more lost")
    print(f"\n  no availability found on the page at all: {len(blank)} "
          f"(these fall through to sequence inference)")
    fb = sorted(c for c, u in units.items() if u["sessions_source"].startswith("page scan"))
    print(f"  read by the permissive fallback, so possibly picked up from prose: "
          f"{len(fb)}")
    if fb:
        print(f"    {fb[:16]}")
    cpr = units.get("41200", {}).get("credit_point_requirements")
    print(f"  [{'ok  ' if cpr else 'FAIL'}] 41200 credit-point rule captured "
          f"({len(cpr)} alternative scopes, all {cpr[0]['min_credit_points']}cp)"
          if cpr else "  [FAIL] 41200 credit-point rule missing")

    print(f"\nRecorded sequence corrections ({SEQ_OVERRIDES}):")
    for m in ov_applied:
        print(f"  applied  {m['code']} {m['major'][:34]:36} "
              f"Y{m['from']['year']} {m['from']['session']} -> "
              f"Y{m['to']['year']} {m['to']['session']}")
    for m in ov_already:
        print(f"  already correct, override now redundant: {m['code']} {m['major'][:34]}")
    for m in ov_missing:
        print(f"  [WARN] not matched: {m['code']} {m['major'][:34]} -> {m['problem']}")
    if not (ov_applied or ov_already or ov_missing):
        print(f"  none (no {SEQ_OVERRIDES} present)")

    exp = data.get("total_credit_points", 198)
    print(f"\nSequence credit points, reconciled against the subject pages "
          f"(handbook total {exp}cp):")
    for m in cp_fixed:
        print(f"  corrected {m['code']} in {m['major'][:34]:36} "
              f"Y{m['year']} {m['session']:7} {m['from_pdf']}cp -> {m['from_subject_page']}cp")
    if not cp_fixed:
        print("  nothing to correct")
    print()
    for maj in sorted(loads):
        L = loads[maj]
        mark = "ok  " if L["matches_handbook_total"] else "FAIL"
        over = L["sessions_over_24cp"]
        note = ""
        if over:
            note = ("   over 24cp: " +
                    ", ".join(f"{k}={v}" for k, v in sorted(over.items())))
        print(f"  [{mark}] {maj[:42]:44} {L['total_credit_points']:>3}cp{note}")
    print("\n  A 27cp session is correct, not an error: 41035 and 41055 are 3cp "
          "subjects.\n  Anything else over 24cp is worth checking by hand.")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
