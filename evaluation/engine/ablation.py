"""Which mechanism absorbs a failure without extending the plan?

Section 4.3.3 credits compaction. Two changes landed together, so each is
switched off in turn and the in-span count re-measured.

  span bound   the retake is placed inside the plan's own span, at the ordinary
               load first and then using the one overloaded period a failure has
               already earned
  compaction   after the cascade, units are pulled earlier where the rules allow
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import os, statistics, sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core
import disruption


def summarise(tag):
    print(f"\n{tag}")
    for inst, rows in disruption.run().items():
        ok = [r for r in rows if r]
        print(f"  {inst:5} in span {sum(1 for r in ok if r[1]==0):5}"
              f"   +1 sem {sum(1 for r in ok if r[1]==1):5}"
              f"   +>=1 year {sum(1 for r in ok if r[1]>=2):5}"
              f"   clean {sum(1 for r in ok if r[0]):5}"
              f"   median moved {statistics.median([r[2] for r in ok])}")


real_place = core._place
real_compact = core.compact_schedule
real_rebalance = core.rebalance_schedule

summarise("both mechanisms on (the engine as it stands)")

core.compact_schedule = lambda rules, s, *a, **k: s
core.rebalance_schedule = lambda rules, s, *a, **k: s
summarise("compaction off, span bound on")

core.compact_schedule = real_compact
core.rebalance_schedule = real_rebalance
core._place = lambda *a, **k: real_place(*a, **{**k, "limit": None})
summarise("span bound off, compaction on")

core.compact_schedule = lambda rules, s, *a, **k: s
core.rebalance_schedule = lambda rules, s, *a, **k: s
summarise("both off")
