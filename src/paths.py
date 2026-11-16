"""
Where every file in the repository lives.

Code refers to a data or results file by its bare name, `P("uts_default_plans.json")`,
and this module says which folder holds it. Moving a file means changing one line
here, not every script that reads it.

USYD and UTS data sit in separate folders on purpose: the two institutions share a
schema but never share a store (Section 4.3 of the thesis).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DATA = ROOT / "data"
RESULTS = ROOT / "results"
FIGURES = RESULTS / "figures"
EVALUATION = ROOT / "evaluation"
ARCHIVE = ROOT / "archive"

_HOME = {
    # Engine inputs: the processed datasets and the standard enrolment plans.
    "degree_facts.json": DATA,
    "merged_degree_data.json": DATA / "usyd",
    "other_engineering_degrees.json": DATA / "usyd",
    "usyd_default_plans.json": DATA / "usyd",
    "usyd_plan_picks.json": DATA / "usyd",
    "uts_c09066_data.json": DATA / "uts",
    "uts_default_plans.json": DATA / "uts",
    "uts_plan_picks.json": DATA / "uts",

    # Scraped and intermediate files the pipeline builds the datasets from.
    "cusp_degree_structure.json": DATA / "sources" / "usyd",
    "stream_table_units.json": DATA / "sources" / "usyd",
    "handbook_results.json": DATA / "sources" / "usyd",
    "unit_sessions.json": DATA / "sources" / "usyd",
    "data_overrides.json": DATA / "sources" / "usyd",
    "usyd_requisites_raw.md": DATA / "sources" / "usyd",
    "usyd_requisites_parsed.json": DATA / "sources" / "usyd",
    "usyd_requisite_fill_log.json": DATA / "sources" / "usyd",
    "table_s_architecture_design_planning_units.json": DATA / "sources" / "usyd",
    "table_s_arts_units.json": DATA / "sources" / "usyd",
    "table_s_business_school_units.json": DATA / "sources" / "usyd",
    "table_s_conservatorium_units.json": DATA / "sources" / "usyd",
    "table_s_engineering_units.json": DATA / "sources" / "usyd",
    "table_s_medicine_health_units.json": DATA / "sources" / "usyd",
    "table_s_science_units.json": DATA / "sources" / "usyd",
    "uts_sequences.json": DATA / "sources" / "uts",
    "sequence_overrides.json": DATA / "sources" / "uts",
    "uts_cache": DATA / "sources" / "uts",

    # Evaluation inputs.
    "profiles.json": EVALUATION / "model",

    # Results. Every evaluation script writes here.
    "plan_run_results.json": RESULTS,
    "baseline_results.json": RESULTS,
    "baseline_results_before.json": RESULTS,
    "consistency_results.json": RESULTS,
    "consistency_fix_replay.json": RESULTS,
    "optimality.json": RESULTS,
    "twofailures.json": RESULTS,
    "metamorphic.json": RESULTS,
    "judge_results.json": RESULTS,
    "chartdata.json": RESULTS,
    "moveddump.json": RESULTS,
}


def P(name):
    """The full path of a known file, as a string."""
    try:
        return str(_HOME[name] / name)
    except KeyError:
        raise KeyError(f"{name} is not registered in src/paths.py") from None
