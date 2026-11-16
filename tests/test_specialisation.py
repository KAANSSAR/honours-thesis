"""
Standalone test for assign_specialisation_electives() against the real,
scraped degree data - separate from curriculum_planner.py's main() demo
flow, so this can be run and iterated on independently.

Run this AFTER scrape_cusp_degree.py and merge_degree_data.py have both
been re-run with the specialisation-scraping update - merged_degree_data.json
needs a non-empty "specialisations" list for this to do anything useful.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "archive"))

import curriculum_planner as cp


def main():
    merged_data = cp.load_merged_data()

    specialisations = merged_data.get("specialisations", [])
    print(f"Loaded {len(specialisations)} specialisations from merged_degree_data.json:")
    for s in specialisations:
        print(f"  - ({s['type']}) {s['name']}  "
              f"[core_block={s['core_block']!r}, elective_block={s['elective_block']!r}]")
    if not specialisations:
        print("\n[WARN] No specialisations found - re-run scrape_cusp_degree.py then "
              "merge_degree_data.py before continuing.")
        return
    print()

    prereqs, coreqs, dependents = cp.build_prereq_graph(merged_data)

    schedule = cp.parse_default_schedule(merged_data)
    schedule = cp.compact_schedule(schedule, merged_data, prereqs, coreqs)
    schedule = cp.rebalance_schedule(schedule, merged_data, prereqs, coreqs)

    # Pick a stream + breadth specialisation to test with - change these
    # to try a different combination, or set one/both to None to test
    # the other 3 combos (e.g. stream-only, breadth-only, neither).
    STREAM_CHOICE = "Internet of Things (IoT)"
    BREADTH_CHOICE = "Humanitarian Engineering"

    print(f"=== Assigning electives: stream={STREAM_CHOICE!r}, breadth={BREADTH_CHOICE!r} ===\n")
    try:
        assigned, report = cp.assign_specialisation_electives(
            schedule, merged_data, prereqs, coreqs,
            stream_specialisation=STREAM_CHOICE,
            breadth_specialisation=BREADTH_CHOICE,
        )
    except ValueError as e:
        print(f"[ERROR] {e}")
        return

    cp.print_schedule(assigned, merged_data)

    print(f"Placed {len(report['placed'])} units:")
    for code, sem_index in report["placed"]:
        name = merged_data["units"].get(code, {}).get("name", "?")
        print(f"  - {code} ({name}) -> Year {sem_index // 2 + 1} Sem {sem_index % 2 + 1}")

    if report["skipped_mandatory_no_room"]:
        print(f"\n[WARN] Mandatory core units that couldn't be placed anywhere: "
              f"{report['skipped_mandatory_no_room']}")
    else:
        print("\nAll mandatory core units placed successfully.")

    print("\nElective pool completion:")
    for pool, info in report["pool_completion"].items():
        used, target = info["used_cp"], info["target_cp"]
        flag = "" if target is None or used >= target else "  [UNDER-FILLED]"
        print(f"  - {pool}: {used}cp / {target}cp{flag}")

    violations = cp.validate_schedule(assigned, prereqs, coreqs, merged_data)
    print()
    if violations:
        print("[WARN] Validation issues after assignment:")
        for v in violations:
            print(f"  - {v}")
    else:
        print("Validation: schedule is internally consistent after elective assignment.")


if __name__ == "__main__":
    main()