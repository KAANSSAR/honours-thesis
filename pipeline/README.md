# Data pipeline

These scripts built the datasets in `data/` from the published university
handbooks. You do not need to run them to use the engine: their outputs are
committed. Run them only to rebuild the data from source. Most need network
access and `requests` and `beautifulsoup4`.

USYD and UTS are built separately and never merged. Every script finds its
files through `src/paths.py`, so each one runs from any working directory.

## University of Sydney

Run in this order:

| Step | Script | Reads | Writes |
|---|---|---|---|
| 1 | `usyd/scale_live_fetch.py` | Software Engineering stream table (web) | `stream_table_units.json`, `handbook_results.json` |
| 2 | `usyd/scrape_cusp_degree.py` | CUSP degree page (web) | `cusp_degree_structure.json` |
| 3 | `usyd/scrape_unit_sessions.py` | each unit's handbook page (web) | `unit_sessions.json` |
| 4 | `usyd/scrape_all_table_s.py` | the seven Table S elective pages (web) | `table_s_<faculty>_units.json` |
| 5 | `usyd/merge_degree_data.py` | steps 1 to 4 | `data/usyd/merged_degree_data.json` |
| 6 | `usyd/scrape_other_engineering_degrees.py` | CUSP pages for the other degrees (web) | `cusp_degree_<id>_<name>.json` |
| 7 | `usyd/apply_overrides.py` | the datasets | corrects them in place, logs to `data_overrides.json` |
| 8 | `usyd/parse_usyd_requisites.py` | `usyd_requisites_raw.md` | `usyd_requisites_parsed.json` |
| 9 | `usyd/apply_usyd_requisites.py` | step 8 | fills requisites in both datasets, logs to `usyd_requisite_fill_log.json` |

Gaps in the record:

- `data/usyd/other_engineering_degrees.json` combines the per-degree files from
  step 6. The script that combined them is not in this repository.
- `usyd_requisites_raw.md` was collected from the handbook's unit-of-study
  tables. Its header says where each line came from.
- `data/usyd/usyd_default_plans.json`, the 42 standard Sydney enrolment plans,
  was written by hand from the published sequences. No script builds it.

## University of Technology Sydney (C09066)

| Step | Script | Reads | Writes |
|---|---|---|---|
| 1 | `uts/parse_uts_sequences_html.py` | the cached C09066 course page | `uts_sequences.json` |
| 2 | `uts/fetch_uts_pages.py` | `uts_sequences.json`, subject and block pages (web) | `uts_cache/` |
| 3 | `uts/reparse_uts2.py` | `uts_cache/`, `uts_sequences.json`, `sequence_overrides.json` | `data/uts/uts_c09066_data.json` |
| 4 | `uts/build_uts_default_plans.py` | step 3, `uts_plan_picks.json` | `data/uts/uts_default_plans.json` |

The page cache, `data/sources/uts/uts_cache/`, is not in this repository,
because the pages belong to UTS. Step 2 rebuilds it. Step 1 expects the C09066
course page to be saved in the cache first.

## Checked

Run against a copy of the cache, steps 1, 3 and 4 for UTS and step 8 for USYD
reproduce the committed files byte for byte. Re-running USYD steps 5, 7 and 9
gives a dataset that adds some empty default fields, and the plan results are
identical.
