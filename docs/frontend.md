# The frontend

Section 4.11 of the thesis. One page over the same API the language model calls.

## Running it

```
python3 src/server.py
```

Then open <http://localhost:8000>.

No install step and no dependencies beyond the standard library. If the engine
runs, this runs.

```
python3 src/server.py --port 9000     # a different port
python3 src/server.py --no-model      # engine views only, no Ollama needed
python3 src/server.py --host 0.0.0.0  # reachable from another machine on the network
```

It binds to loopback by default, so the dataset is not served to the network
unless that is asked for.

## The three views

**Plan.** Pick a university, a degree and a specialisation combination, and the
plan is drawn years down, teaching periods across, which is how both handbooks
print it. Clicking a unit shows its rules. "Check this plan" runs the validator
and reports what it finds, including the conflicts the published sequences
really contain.

The one worth demonstrating: choose a unit under "If you failed a unit". The
grid redraws with every moved unit marked, the failed unit in red, and if the
repair cannot be absorbed within the degree's nominal length, an extra year
appears.

**Compare degrees.** The seven paired degrees. Facts are on shared rows so a
difference can be read across rather than hunted for. The credit-point split
is below the table and not on shared rows, because the two institutions publish
different categories and forcing them together would invent a correspondence
that does not exist. "Open this plan" moves to the plan view for that degree.

**Ask.** The conversation. Needs Ollama running with qwen3:8b. The model's
prose is on the left and the engine's structured answer on the right, so which
half came from where is visible. This is the only view that needs the model.

## What is where

```
src/server.py          the HTTP layer: routes, static files, no rules
src/static/index.html  the page
src/static/app.css     tokens and layout
src/static/app.js      draws what the engine returns, decides nothing
```

## The boundary

The page holds no rule. It does not know what a prerequisite is, what a valid
plan is, or how many credit points are too many: that last one is sent by the
institution's rule set with the plan, because the two institutions agreeing on
30 today is a fact about the data rather than a guarantee. Everything that
decides correctness is behind the API, which is the same boundary Section 4.10
draws around the language model. A bug in the page can misdraw an answer. It
cannot manufacture one.

There is no session and no stored student. Two visits with the same selection
produce the same screen.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /api/health` | plan counts, and whether the model is enabled |
| `GET /api/plans` | every plan, grouped by university and degree |
| `GET /api/plan?institution=&key=` | the plan, with unit names and elective slots |
| `GET /api/plan/units?institution=&key=` | the units in a plan, for the failed-unit picker |
| `GET /api/validate?institution=&key=` | the validator's verdict |
| `GET /api/replan?institution=&key=&failed_unit=` | the repaired plan, with moves marked |
| `GET /api/pairs` | the Path A degree pairings |
| `GET /api/compare?usyd_key=&uts_key=` | the Path A comparison |
| `GET /api/unit?institution=&code=` | a unit's rules |
| `POST /api/ask` `{"message": "..."}` | the full exchange: profile, tool call, result, reply |

Every one of these is the engine's own function, reached the same way the model
reaches it.
