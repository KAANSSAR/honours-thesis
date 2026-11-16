"""Dump units moved and span growth for every single-failure repair, for the
units-moved chart."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluation" / "engine"))
import disruption
from paths import P
out = {}
for inst, rows in disruption.run().items():
    out[inst] = {"moved": [r[2] for r in rows if r],
                 "grew": [r[1] for r in rows if r]}
json.dump(out, open(P("moveddump.json"), "w"))
for k, v in out.items():
    print(k, len(v["moved"]), "max", max(v["moved"]))
