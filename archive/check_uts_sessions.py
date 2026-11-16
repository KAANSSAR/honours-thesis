"""
Settle whether the six session mismatches are real, or an artefact of how
sessions_offered is extracted.

    python3 check_uts_sessions.py

Reads uts_cache/ only. Writes nothing. No network access.

The problem. Six placements across three majors put a subject in a session the
subject page does not list. Two of them make the extraction itself look suspect
rather than the handbook:

  41059  page says Spring only, but Mechanical AND Mechatronic both place it in
         Year 2 Autumn. Two independent sequences voting against the page.
  41039  page says Autumn only. Software places it in Autumn, Mechatronic in
         Spring, so between them the two sequences claim both sessions.

sessions_offered is built by scanning the whole page text for "<Season> session".
That is a guess, and it fails in both directions. A false positive: the phrase
appears in prose ("assessment is due in week 6 of Spring session") and is read as
availability. A false negative: the availability table words it some other way, so
a real offering is missed. Either produces exactly the mismatches seen, and the
summary counts cannot tell them apart, which is the same trap as every other fault
in this dataset.

So this prints each match with its surrounding text and says which part of the page
it came from. Reading that output answers the question directly:

  - matches sitting in an availability or offering block, and nothing else
    -> extraction is sound, and the handbook's own sequence is inconsistent,
       which is a finding worth reporting
  - matches sitting in prose, or an availability block wording things differently
    -> extraction is wrong, sessions_offered needs a targeted parse of that block,
       and no claim should be made about the handbook
"""
import glob, os, re, sys
from bs4 import BeautifulSoup

CACHE = "uts_cache"
SEASON = re.compile(r"\b(Autumn|Spring|Summer|Winter|December|January|July)\s+session\b")

# The six mismatched placements, and the two sequences that disagree with the page.
SUSPECT = {
    "41059": "Mechanical Y2 Autumn and Mechatronic Y2 Autumn both place it; page says Spring",
    "41039": "Software places it Autumn, Mechatronic places it Spring; page says Autumn",
    "41057": "Mechanical Y2 Autumn; page says Spring",
    "91161": "Biomedical Y2 Spring; page says Autumn",
    "41163": "Biomedical Y3 Autumn; page says Spring",
    "41200": "control case: page genuinely prints no availability at all",
}

# Headings and labels that would introduce a real availability block.
AVAIL_HINT = re.compile(r"availability|when offered|offering|typical availability|"
                        r"session|campus|mode of delivery", re.IGNORECASE)


def cached(code):
    for p in (f"*subjects_{code}_html", f"*subjects_details_{code}_html"):
        hits = glob.glob(os.path.join(CACHE, p + ".html")) or glob.glob(os.path.join(CACHE, p))
        if hits:
            return hits[0]
    hits = [f for f in os.listdir(CACHE) if code in f]
    return os.path.join(CACHE, hits[0]) if hits else None


def region(soup, code):
    """Report which structural part of the page each season phrase sits in."""
    out = []
    for el in soup.find_all(string=SEASON):
        text = re.sub(r"\s+", " ", str(el)).strip()
        # Walk up to the nearest element that names what this is.
        label, node, depth = "", el.parent, 0
        while node is not None and depth < 6:
            nm = getattr(node, "name", None)
            if nm in ("table", "dl", "ul", "section", "div", "td", "th", "dd", "dt", "li"):
                head = node.find_previous(["h1", "h2", "h3", "h4", "h5", "strong", "b", "th", "dt"])
                label = re.sub(r"\s+", " ", head.get_text(" ", strip=True))[:48] if head else ""
                label = f"<{nm}> under {label!r}" if label else f"<{nm}>"
                break
            node, depth = node.parent, depth + 1
        out.append((label or "(loose text)", text))
    return out


def main():
    if not os.path.isdir(CACHE):
        sys.exit(f"{CACHE}/ not found. Run this from the folder holding uts_cache/.")

    for code, why in SUSPECT.items():
        path = cached(code)
        print("=" * 78)
        print(f"{code}   {why}")
        if not path:
            print("  not in cache\n")
            continue
        html = open(path, encoding="utf-8", errors="ignore").read()
        soup = BeautifulSoup(html, "html.parser")
        flat = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

        hits = SEASON.findall(flat)
        print(f"  whole-page scan finds: {sorted(set(hits)) or 'nothing'}  "
              f"({len(hits)} occurrences)")

        placed = region(soup, code)
        if not placed:
            print("  no season phrase anywhere in the markup")
        for label, text in placed[:10]:
            print(f"\n  in {label}")
            print(f"    {text[:150]}")
        if len(placed) > 10:
            print(f"\n  ... {len(placed) - 10} more occurrences")

        # Does the page even have something that looks like an availability block?
        heads = [re.sub(r"\s+", " ", h.get_text(" ", strip=True))
                 for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "dt", "th", "strong"])]
        av = [h for h in heads if AVAIL_HINT.search(h) and len(h) < 60]
        print(f"\n  availability-looking labels on the page: {av[:6] or 'none'}")
        print()

    print("=" * 78)
    print("How to read this:")
    print("  Every match inside an availability or offering block, and none in prose,")
    print("  means the extraction is sound and the handbook sequence is inconsistent.")
    print("  Matches in prose, or an availability block that words sessions some other")
    print("  way, means the extraction is at fault and no claim should be made about")
    print("  the handbook. 41200 is the control: its page prints no availability, so")
    print("  it should show no availability block and, ideally, no matches at all.")


if __name__ == "__main__":
    main()
