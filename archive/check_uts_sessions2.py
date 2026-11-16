"""
Resolve one contradiction: 41057 and 41059 carry "Spring session" in the dataset,
but scanning their cached pages finds no season phrase at all.

    python3 check_uts_sessions2.py

Reads uts_cache/ and uts_c09066_data.json. Writes nothing. No network access.

Both numbers cannot be right. Either the parser read a different file than the
diagnostic did, or the availability block words sessions in a way the scan misses,
or the value came from somewhere other than the page. This prints enough to tell
which, and nothing else:

  - the exact cached file each lookup resolves to, and whether they agree
  - what the dataset currently holds, including whether it was inferred
  - the full text of the Typical availability region, verbatim

41163 is included as a positive control: its page is known to contain
"Spring session, City campus", so whatever this script prints for 41163 is what a
working case looks like.
"""
import glob, json, os, re, sys
from bs4 import BeautifulSoup

CACHE = "uts_cache"
DATA = "uts_c09066_data.json"
CODES = ["41057", "41059", "41163"]
SEASON = re.compile(r"\b(Autumn|Spring|Summer|Winter|December|January|July)\b")
AVAIL = re.compile(r"Typical availability|Availability", re.IGNORECASE)


def resolve_reparse(code):
    """The lookup reparse_uts2.py uses."""
    for p in (f"*subjects_{code}_html", f"*subjects_details_{code}_html",
              f"*directory_{code.lower()}_html"):
        hits = glob.glob(os.path.join(CACHE, p + ".html")) or glob.glob(os.path.join(CACHE, p))
        if hits:
            return sorted(hits)
    return sorted(os.path.join(CACHE, f) for f in os.listdir(CACHE)
                  if code.lower() in f.lower())


def resolve_diagnostic(code):
    """The lookup check_uts_sessions.py uses."""
    for p in (f"*subjects_{code}_html", f"*subjects_details_{code}_html"):
        hits = glob.glob(os.path.join(CACHE, p + ".html")) or glob.glob(os.path.join(CACHE, p))
        if hits:
            return sorted(hits)
    return sorted(os.path.join(CACHE, f) for f in os.listdir(CACHE) if code in f)


def main():
    if not os.path.isdir(CACHE):
        sys.exit(f"{CACHE}/ not found. Run this from the folder holding uts_cache/.")
    units = json.load(open(DATA))["units"] if os.path.exists(DATA) else {}

    for code in CODES:
        a, b = resolve_reparse(code), resolve_diagnostic(code)
        u = units.get(code, {})
        print("=" * 78)
        print(f"{code}  {u.get('name', '')}")
        print(f"  dataset says: sessions={u.get('sessions_offered')} "
              f"inferred={u.get('sessions_inferred')} "
              f"source={u.get('sessions_source')!r}")
        print(f"  reparse resolves to:    {[os.path.basename(x) for x in a]}")
        print(f"  diagnostic resolves to: {[os.path.basename(x) for x in b]}")
        print(f"  same file: {a[:1] == b[:1]}"
              + ("" if a[:1] == b[:1] else "   <<< this alone explains the contradiction"))
        if not a:
            print("  nothing in cache\n")
            continue

        path = a[0]
        print(f"  size: {os.path.getsize(path)} bytes")
        soup = BeautifulSoup(open(path, encoding="utf-8", errors="ignore").read(),
                             "html.parser")
        flat = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        print(f"  page length: {len(flat)} chars")
        print(f"  bare season words anywhere: {sorted(set(SEASON.findall(flat))) or 'none'}")
        print(f"  the phrase '<Season> session': "
              f"{sorted(set(re.findall(r'(\w+)\s+session', flat))) or 'none'}")

        # The availability region, verbatim, so its real wording is visible.
        for m in AVAIL.finditer(flat):
            print(f"\n  ...{flat[max(0, m.start() - 60):m.start() + 320]}...")
            break
        else:
            print("\n  no availability region in the text at all")
        print()

    print("=" * 78)
    print("How to read this:")
    print("  'same file: False'  -> the two lookups disagree, the cache lookup is")
    print("                         ambiguous, and that is the bug to fix.")
    print("  same file, but the availability region names no session -> the dataset")
    print("                         value did not come from this page, so it is wrong.")
    print("  same file, and the region names a session in other words -> the scan")
    print("                         pattern is too narrow and needs that wording.")
    print("  41163 shows what a working case looks like, for comparison.")


if __name__ == "__main__":
    main()
