"""
Institution-scoped dataset loading for the curriculum planner.

USYD and UTS are kept in SEPARATE stores sharing one schema. Nothing in this module
merges unit dictionaries across institutions: a requisite that cannot be resolved
inside its own institution's dataset is a violation, never a hit in the other one.
Path A is the only consumer that opens both at once, and it compares rather than
merges.

    usyd = load("USYD", "Software Engineering")
    uts  = load("UTS",  "Software Engineering major")
    usyd.units["INFO1110"]          # present
    uts.units.get("INFO1110")       # None, by construction
"""
import json, os, re
from collections import defaultdict

from paths import P

# Absolute paths, so the `data_dir` arguments below no longer change anything.
USYD_SOFTWARE = P("merged_degree_data.json")
USYD_OTHERS = P("other_engineering_degrees.json")
UTS_DATA = P("uts_c09066_data.json")

# USYD degree name -> degree_id in other_engineering_degrees.json.
# Software Engineering lives in its own file and has no id here.
USYD_DEGREES = {
    "Software Engineering": None,
    "Aeronautical": "442", "Aeronautical with Space": "445",
    "Biomedical": "456", "Chemical and Biomolecular": "455",
    "Civil": "448", "Electrical": "449", "Environmental": "1235",
    "Mechanical": "443", "Mechanical with Space": "446",
    "Mechatronic": "444", "Mechatronic with Space": "447",
}

# Block name roles. "core" and "elective" are the original two; "selective" (Civil
# Structures) and "breadth units" (Mechanical Industrial and Product Design) were
# found while authoring the default plans, and a specialisation may also be a single
# combined block with no role suffix at all (Biocomputation, Water Engineering,
# Thermofluids, Mechanical Design, Materials Science, Aerospace Research).
BLOCK_ROLES = (("Core Units", "core"), ("Selective Units", "selective"),
               ("Elective Units", "elective"), ("Breadth Units", "breadth"),
               ("Units", "combined"))


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _strip_suffix(name):
    """'Humanitarian Engineering (Civil)' -> 'Humanitarian Engineering'."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()


def resolve_blocks(spec_name, block_names):
    """
    Map a specialisation name to its blocks, keyed by role.

    The scraper's core_block/elective_block fields come back None whenever the block
    name does not match the specialisation name exactly, which happens in four ways:

      1. a single combined block, "Specialisation - Water Engineering Units", with no
         Core/Elective pair at all;
      2. a disambiguating suffix in the block name only, "Internet of Things (IoT)"
         against "Specialisation - Internet of Things (Electrical) Core Units";
      3. a suffix in the specialisation name only, "Telecommunications Engineering"
         against "Specialisation - Telecommunications Core Units", and likewise
         "Humanitarian Engineering (Civil)";
      4. headings that read "Specialisation in X" rather than "Stream Specialisation
         in X", which dropped all three Environmental streams.

    Matching here is on the normalised name with bracketed suffixes stripped from both
    sides, so all four resolve. Returns {role: block_name}.
    """
    want = _norm(_strip_suffix(spec_name))
    found = {}
    for bn in block_names:
        if "specialisation" not in bn.lower():
            continue
        body = re.sub(r"^\s*Specialisation\s*[-:]\s*", "", bn, flags=re.IGNORECASE)
        role = "combined"
        for suffix, r in BLOCK_ROLES:
            if body.strip().endswith(suffix):
                body = body[: body.rfind(suffix)]
                role = r
                break
        got = _norm(_strip_suffix(body.strip()))
        if not got or not want:
            continue
        if got == want or got.startswith(want) or want.startswith(got):
            found.setdefault(role, bn)
    return found


class Dataset:
    """One institution's units, blocks and degree structure. Never merged."""

    def __init__(self, institution, degree, units, blocks, semester_plan,
                 specialisations, meta=None):
        self.institution = institution
        self.degree = degree
        self.units = units
        self.blocks = blocks
        self.semester_plan = semester_plan
        self.specialisations = specialisations
        self.meta = meta or {}
        for code, u in self.units.items():
            u.setdefault("institution", institution)

    def __repr__(self):
        return (f"<Dataset {self.institution}/{self.degree} "
                f"units={len(self.units)} blocks={len(self.blocks)}>")

    def check_institution(self, code):
        """Raise rather than silently resolve a code from the wrong institution."""
        u = self.units.get(code)
        if u is None:
            return None
        if u.get("institution") != self.institution:
            raise ValueError(f"{code} belongs to {u.get('institution')}, "
                             f"not {self.institution}")
        return u

    def specialisation_blocks(self, name):
        return resolve_blocks(name, self.blocks.keys())

    def block_units(self, block_name):
        return set(self.blocks.get(block_name, {}).get("members", ()))


def _usyd_software(data_dir):
    j = json.load(open(os.path.join(data_dir, USYD_SOFTWARE)))
    blocks = defaultdict(lambda: {"max_cp": None, "min_cp": None, "members": set()})
    for code, u in j["units"].items():
        for b in (u.get("degree_blocks") or []):
            blocks[b]["members"].add(code)
    for b, m in (j.get("degree_blocks_meta") or {}).items():
        blocks[b]["max_cp"] = m.get("max_cp")
        blocks[b]["min_cp"] = m.get("min_cp")
    return Dataset("USYD", "Software Engineering", dict(j["units"]), dict(blocks),
                   j["semester_plan"], j.get("specialisations") or [],
                   {"source": USYD_SOFTWARE})


def _usyd_other(data_dir, degree):
    did = USYD_DEGREES[degree]
    v = json.load(open(os.path.join(data_dir, USYD_OTHERS)))[did]
    units, blocks = {}, {}
    for bn, b in v["unit_blocks"].items():
        blocks[bn] = {"max_cp": b.get("max_cp"), "min_cp": b.get("min_cp"),
                      "members": {u["code"] for u in b.get("units", [])}}
        for u in b.get("units", []):
            rec = units.setdefault(u["code"], dict(u))
            rec.setdefault("degree_blocks", [])
            rec["degree_blocks"].append(bn)
    return Dataset("USYD", degree, units, blocks, v["semester_plan"],
                   v.get("specialisations") or [],
                   {"source": USYD_OTHERS, "degree_id": did,
                    "source_url": v.get("source_url")})


def _uts(data_dir, major):
    path = os.path.join(data_dir, UTS_DATA)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{UTS_DATA} not found. Run scrape_uts_c09066.py first.")
    j = json.load(open(path))
    seqs = j.get("sequences") or {}
    if major and major not in seqs:
        raise KeyError(f"UTS major {major!r} not in dataset. Available: {sorted(seqs)}")
    blocks = {code: {"max_cp": b.get("max_cp"), "min_cp": None,
                     "members": {u["code"] for u in b.get("units", [])},
                     "name": b.get("name")}
              for code, b in (j.get("blocks") or {}).items()}
    return Dataset("UTS", major, dict(j.get("units") or {}), blocks,
                   seqs.get(major, []), [],
                   {"source": UTS_DATA, "course_code": j.get("course_code"),
                    "handbook": j.get("source"), "structure": j.get("structure"),
                    "data_gaps": j.get("data_gaps")})


def load(institution, degree=None, data_dir="."):
    """Load one institution's dataset. Institutions are never combined."""
    inst = (institution or "").upper()
    if inst == "USYD":
        if degree not in USYD_DEGREES:
            raise KeyError(f"Unknown USYD degree {degree!r}. "
                           f"Known: {sorted(USYD_DEGREES)}")
        return (_usyd_software(data_dir) if USYD_DEGREES[degree] is None
                else _usyd_other(data_dir, degree))
    if inst == "UTS":
        return _uts(data_dir, degree)
    raise KeyError(f"Unknown institution {institution!r}; expected USYD or UTS")


def load_pair(usyd_degree, uts_major, data_dir="."):
    """For Path A only: both datasets open at once, side by side, never merged."""
    return load("USYD", usyd_degree, data_dir), load("UTS", uts_major, data_dir)
