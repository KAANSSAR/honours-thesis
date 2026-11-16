# Archive

Earlier versions and one-off investigation scripts, kept so the history the thesis
describes can be checked. None of them is part of the final system, and most
will not run as they stand, because they expect the old flat layout where code
and data sat in one folder.

| File | What it was |
|---|---|
| `curriculum_planner.py` | The original single-file planner. The engine in `src/` was lifted from it with behaviour held fixed. Still used by `tests/test_vs_original.py` and `tests/test_specialisation.py`, so it is kept in working order. |
| `chat_query.py` | An early chat prototype over the scraped USYD stream table, from before the engine existed. Not used by the final system. |
| `parse_uts_sequences.py` | The first UTS sequence parser, which read the course page as PDF text. Replaced by `pipeline/uts/parse_uts_sequences_html.py`. |
| `reparse_uts.py` | The first UTS re-parser. Replaced by `pipeline/uts/reparse_uts2.py`, which fixes five faults found by auditing this one's output. |
| `check_uts_sessions.py`, `check_uts_sessions2.py` | One-off checks on whether six UTS session mismatches were real or an extraction artefact. |
| `check_figures.py` | Arithmetic checks on an earlier draft's figures. The numbers in it are out of date. |
| `probe_model.py` | A debugging aid for empty model replies. Not part of the evaluation. |
| `fetch_test.py` | A first test that the handbook could be fetched at all. |
