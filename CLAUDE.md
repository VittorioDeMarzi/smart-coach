# smart-coach

A running training-plan generator: it reads Garmin data, writes a plan, validates it
against deterministic guardrails, and publishes it to intervals.icu, which syncs it to
the Garmin watch.

**Design:** `docs/design.md` — code comments cite it as `spec <section>`.

## Project constraints

- **No git operations by Claude.** The user runs them.
- **No Strava data in the pipeline**, in any form (spec 3.1).
- **`guardrails.py` is deterministic code**: no training rule is implemented as a
  prompt. A constraint verified by an LLM is not a constraint.
- **Fail closed**: a guardrail whose data is missing is replaced by the stricter
  fallback, not switched off. `skip_check()` is a bug.
- **`None` means missing, never zero**: an absent metric and a zero metric lead to
  opposite decisions.
- **0 EUR/month**: no paid service, no cloud routine.
- **Only the coming week is published.** Without `--week` the target is the ISO week
  after today; if the plan does not hold it, the command refuses instead of guessing.
- **`plan.md` is the single source of truth.** It holds the whole cycle; the Google
  Sheet is a one-way view, overwritten by `export` and never read back.
- Models are `@dataclass(frozen=True)`, collections are `tuple`.
- **Everything in the repository is in English**: code, comments, documentation, command
  output and the keywords of `plan.md`. The coach talks to the athlete in the athlete's
  own language, and writes plan prose and workout names in it.

## Files

| File | What it holds |
|---|---|
| `config/rules.toml` | **all** the tunable rules: regime, guardrails. Nothing about an athlete |
| `config/athlete.example.toml` | the empty model for `data/athlete.toml` |
| `data/athlete.md` | stable profile: injuries, constraints, preferences, personal bests |
| `data/athlete.toml` | this athlete's numbers: baseline floor, periods run without recording |
| `data/goal.md` | the goal of the current cycle |
| `data/plan.md` | the plan, the whole cycle. Before rewriting it the skill shows what changes: that is the human veto |

`config/` travels with the code. `data/` belongs to the athlete and is entirely
git-ignored (only its `README.md` is tracked): see below.

## Another athlete

The repository holds code, rules and the skill only. An athlete's data **stays on their
machine** and is git-ignored: `.env`, everything in `data/`, plus the cached Garmin
session. Another
athlete runs `git clone`, fills in their own `.env` and starts `/coach`: the skill
creates the files.

- **No backup and no git history of the plan.** The current plan survives on the Google
  Sheet and on intervals.icu; the profile is rebuilt by the intake. Whoever wants history
  and a backup runs `git init` inside `data/` and keeps it as a private repository of
  its own.
- **Never add anything in `data/` to git**, not even with `-f`.
- Two athletes on the same computer must each have their own `GARMIN_TOKEN_STORE` in
  `.env`: cached tokens win over email and password, so with a shared folder the second
  would silently read the first one's runs.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env   # then fill it in: credentials are read from here
cp config/athlete.example.toml data/athlete.toml   # optional: without it, no floor and no data gap
```

The editable install is required: without it `src/` is on the path only inside pytest
and the commands below fail with `ModuleNotFoundError`.

## Commands

```bash
.venv/bin/python -m smart_coach.cli snapshot
.venv/bin/python -m smart_coach.cli validate            # the COMING week
.venv/bin/python -m smart_coach.cli validate --week 40  # a specific week
.venv/bin/python -m smart_coach.cli validate --all      # + the shape of the rest of the cycle
.venv/bin/python -m smart_coach.cli push --week 40 --year 2027
.venv/bin/python -m smart_coach.cli export              # plan.md -> Google Sheet (no Garmin needed)
.venv/bin/python -m pytest
```
