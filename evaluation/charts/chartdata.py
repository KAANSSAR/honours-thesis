"""Dump the data behind the three Chapter 4 charts, so the figures are built
from the engine rather than from numbers retyped out of the tables."""
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import api, core
import rules as R
from paths import P

eng = api.Engine()
out = {"loads": {"USYD": [], "UTS": []}, "plan_loads": []}

for inst in ("USYD", "UTS"):
    rules = eng.rules[inst]
    for key, plan in eng.plans.get(inst, {}).items():
        data = eng._dataset_for(inst, plan)
        sched = eng._schedule_for(inst, plan)
        per = []
        for i in sorted(sched):
            if not sched[i]:
                continue
            cp = core.period_credit_points(rules, sched, i, data)
            out["loads"][inst].append(cp)
            per.append(cp)
        out["plan_loads"].append({"institution": inst, "key": key, "loads": per})

# unit sizes, which is the other half of the lattice claim
sizes = {"USYD": collections.Counter(), "UTS": collections.Counter()}
usyd_units = {}
for d, v in eng.data["USYD"].items():
    usyd_units.update(v["units"])
for inst, units in (("USYD", usyd_units), ("UTS", eng.data["UTS"]["units"])):
    for u in units.values():
        cp = u.get("credit_points")
        if cp:
            sizes[inst][cp] += 1
out["unit_sizes"] = {k: dict(v) for k, v in sizes.items()}

json.dump(out, open(P("chartdata.json"), "w"))
for inst in ("USYD", "UTS"):
    c = collections.Counter(out["loads"][inst])
    print(inst, "periods:", len(out["loads"][inst]), "load histogram:", dict(sorted(c.items())))
    print("   unit sizes:", dict(sorted(sizes[inst].items())))
