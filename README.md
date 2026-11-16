# A Deterministic Planning Engine for Engineering Degrees at USYD and UTS

Honours thesis, Bachelor of Engineering (Software) Honours, University of Sydney, 2026.
Kanishka Sarkar, supervised by Dr Nicholas Tse.

The full thesis is in [`docs/thesis-b-final.pdf`](docs/thesis-b-final.pdf).

## What this is

A student who fails a unit has to rebuild their degree plan. Every later unit
that depended on the failed one has to move, each move has to respect
prerequisites, corequisites, session availability, credit-point thresholds and
the load limit, and the result should finish as early as the rules allow.
Language models are bad at this: they produce plausible plans that quietly
break rules.

This repository splits the job in two:

- **A rule-based engine** builds, checks and repairs enrolment plans. It is
  deterministic, so the same request always gets the same answer, and every
  plan it returns is checked against the published rules.
- **A thin language-model layer** (Qwen3-8B through Ollama) turns a student's
  question into a call to the engine and explains the result. It never decides
  whether a plan is valid.

One engine serves two universities, the University of Sydney (42 standard
plans across eight engineering degrees) and UTS course C09066 (nine plans). The
two institutions share a data format but never share data.

## Results

Every figure below is produced by a script in this repository. The logs of the
runs are in [`results/logs/`](results/logs).

| Question | Result | Script |
|---|---|---|
| Do the published plans meet the rules? | 51 of 51, with 6 findings that come from the published material itself | `src/run_all_plans.py` |
| Does every single-failure repair meet the rules? | 1,497 of 1,497 | `src/run_all_plans.py --replan` |
| Does an independently written checker agree? | 1,497 of 1,497 clean, no new violations | `evaluation/engine/audit.py --replan` |
| Does a CP-SAT constraint model agree on altered plans? | 551 of 600 (91.8%) | `evaluation/engine/differential.py 600` |
| Is the repair the shortest possible? | Proved shortest in 1,307 of 1,393 judged scenarios (93.8%) | `evaluation/engine/optimality.py` |
| Would the checker notice a broken engine? | 6 of 9 planted faults caught; the 3 survivors are explained in the thesis | `evaluation/engine/mutation.py` |
| Do two failures in one plan still repair cleanly? | 2,098 of 2,098 pairs | `evaluation/engine/twofailures.py` |
| Do metamorphic relations hold? | 10,479 of 10,479 checks | `evaluation/engine/metamorphic.py` |
| Does set iteration order change any answer? | No, identical across hash seeds | `evaluation/engine/orderdep.py` |
| Does a student's background change the engine's answer? | No, 0 of 8 scenarios vary across 15 personas | `evaluation/model/eval_consistency.py --backend engine-only` |
| Are the model's explanations faithful to the engine? | 24 of 41 judged faithful | `evaluation/model/judge.py` |

The optimality split between "a shorter plan exists" and "the model's witness
failed validation" can move by one or two scenarios between runs, because the
solver is multithreaded. The proved-optimal count does not move.

## Repository layout

```
src/                 the system
  core.py              the solver; never knows which university it is working on
  rules.py             the interface the solver asks its questions through
  rules_usyd.py        how a Sydney degree answers those questions
  rules_uts.py         how UTS C09066 answers them
  datasets.py          loads one institution's data, never both at once
  api.py               the operations the frontend and the model both call
  pathA.py, metrics.py degree comparison for prospective students
  llm_layer.py         intake, tool calls and explanation; nothing else
  run_all_plans.py     validates every plan, and every repair with --replan
  server.py, static/   the web frontend
  chat.py              the same conversation from the terminal
  paths.py             where every data and results file lives

data/                the datasets the engine reads
  usyd/                Sydney units, degree structures and standard plans
  uts/                 UTS C09066 units, structure and standard plans
  sources/             scraped and intermediate files the datasets are built from
  degree_facts.json    fees and admin facts for the comparison, with sources

pipeline/            scrapers and parsers that built data/ (see pipeline/README.md)
evaluation/
  engine/              verification of the engine: independent checker, CP-SAT
                       model, mutation testing, metamorphic and order tests
  model/               evaluation of the language layer: baseline, consistency,
                       bias probe, LLM-as-judge
  charts/              the figures in the thesis
tests/               tests that run without a language model
results/             outputs of the evaluation, figures and run logs
docs/                the thesis, the Thesis A report and frontend notes
archive/             earlier versions and one-off scripts (see archive/README.md)
```

## Running it

Python 3.12 or later. The engine, the frontend and the tests use only the
standard library. Every script finds its files through `src/paths.py`, so you
can run it from any folder.

```bash
# validate the 51 plans, and every single-failure repair
python3 src/run_all_plans.py
python3 src/run_all_plans.py --replan

# the web frontend, without the language model
python3 src/server.py --no-model
# then open http://localhost:8000

# the tests
python3 tests/test_llm_layer.py
```

### With the language model

Install [Ollama](https://ollama.com), then:

```bash
ollama pull qwen3:8b
python3 src/server.py                 # the Ask tab now works
python3 src/chat.py "I failed Programming 1 in my UTS software degree"
```

### The evaluation

```bash
pip install -r requirements.txt       # ortools and matplotlib
python3 evaluation/engine/audit.py --replan
python3 evaluation/engine/optimality.py
python3 evaluation/engine/mutation.py
```

`evaluation/model/eval_baseline.py` and `eval_consistency.py --backend ollama`
need the model running and take a long time. Their recorded results are in
`results/`.

## Notes

- The data is a snapshot of the 2026 handbooks. Rules and session offerings
  change every year, so the plans here are not enrolment advice.
- The cached UTS handbook pages are not included, because they belong to UTS.
  `pipeline/uts/fetch_uts_pages.py` rebuilds them.
- `data/sources/usyd/` holds scraped Sydney handbook data. The provenance of
  each dataset is in `pipeline/README.md`.

## Licence

The code is released under the [MIT licence](LICENSE). It does not cover the
thesis documents in `docs/`, or the handbook data in `data/`, which comes from
the University of Sydney and UTS.
