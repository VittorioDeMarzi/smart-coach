# smart-coach — design

How the system is built and why each rule exists. Code comments cite this document as
`spec <section>`, so the section numbers are stable: some numbers are skipped on purpose.

---

## 1. Context and goal

Replace a paid training-plan subscription with a personal system that generates and
adapts a running plan, using the athlete's real data and Claude as the decision engine.

**Expected outcome:** open the watch in the morning and find today's session already on
it, fully structured (warm-up, repeats, targets, cool-down).

**What success means:**

1. The plan comes from a short intake, not from a generic questionnaire.
2. Today's session is on the watch with no daily manual action.
3. The plan changes in response to real data (load, adherence, how the athlete feels),
   not only to the calendar.
4. Additional recurring cost: **0 EUR/month**.

It is a single-athlete tool that runs on the athlete's own computer. It is not a service.

---

## 2. Non-goals

Each of these was considered and dropped, not forgotten.

| Non-goal | Why |
|---|---|
| Web UI / app | The chat is already the interface. A UI adds a front end to maintain and enables nothing new. The Google Sheet (section 9) is a read-only view, not a UI. |
| Database | The coach works on text files, and one more service to keep alive at 0 EUR for a single athlete enables nothing. |
| Multi-sport | Other sports enter as constraints on the running week, never as planned sessions. |
| Backfill of history from other platforms | The only realistic source would be Strava, excluded by its policy (3.1), and Garmin Connect has no bulk import. What matters comes from the athlete: personal bests, injury history, and the periods run without recording (8.5). |
| Running in the cloud | Garmin is unreachable from datacentres (3.4), and the athlete's data stays on their machine (section 5). |

---

## 3. Constraints

### 3.1 Strava is not a data source

The only data source is **Garmin**. Strava does not enter the pipeline, neither for
reading nor as a historical backfill.

**Reason: Strava's API policy.** The policy in force since 1 June 2026 forbids using
Strava data in any "AI Application", explicitly naming "ingestion into a context window
or working memory", and the restriction extends to derived data. Strava makes an
exception for its own official MCP connector, which is read-only. This system does not
read, import or process Strava data in any form, so the policy is met by construction.

There is no technical reason on top of that one. The training load is derived from the
duration and heart rate of each run (section 6), which Strava has too: a Strava history
would genuinely cover a period Garmin did not record. That case is handled differently,
by the athlete declaring the period (8.5).

Garmin is also the richer source: it exposes sleep, HRV, resting heart rate and, on the
models that compute it, training load.

### 3.2 Cost — 0 EUR/month

| Component | Cost |
|---|---|
| Claude Code, within an existing Claude subscription | no extra |
| intervals.icu with a personal API key | 0 |
| Garmin Connect | 0 |
| Google Sheets API with a service account | 0 |

Nothing in the design depends on a scheduler: the weekly run takes about two minutes in
chat, so automating it is a convenience, not a capability.

### 3.4 Network — Garmin is unreachable from a datacentre

Verified on 2026-09-27 with a probe on GitHub Actions, using deliberately invalid
credentials: Garmin did not answer "wrong credentials" but blocked the request **before**
evaluating them, with an HTTP 403 bot challenge and 429 on every login strategy. From
the same runner, intervals.icu answered `401 Unauthorized` with a regular JSON body.

The difference is one of intent: Garmin defends its login from automation and has no
public API for personal use; intervals.icu publishes an API with personal keys.

**Consequence:** reading Garmin, and therefore validating and adapting the plan, can
only run from a residential network — the athlete's own computer. A VPS has a datacentre
address and is excluded for the same reason.

**Corollary:** the weekly run must **tolerate delay**. A laptop is not a reliable
scheduler; if the run does not happen on Sunday it must be able to happen on Wednesday
and adapt the days that are left (7.7).

---

## 4. Architecture

```
   Garmin Connect ──read──▶ garmin_reader ──▶ Snapshot ──▶ regime
                                                  │
   data/athlete.md  ─┐                            ▼
   data/goal.md     ─┼──▶  Claude (skill: coach) ──writes──▶ data/plan.md
   data/athlete.toml ┘                                            │
                                                                  ▼
                                   guardrails.validate ◀── config/rules.toml
                                                                  │ no blocking violation
                                                                  ▼
                                   intervals_pusher ──▶ intervals.icu ──▶ Garmin watch
                                   sheet_exporter   ──▶ Google Sheet (read-only view)
```

### 4.1 Why intervals.icu sits in the middle

Structured workouts can reach a Garmin watch in two ways: directly, through the
unofficial client of Garmin's web API, or through intervals.icu, which has a documented
public API and an official integration that sends planned workouts to Garmin by itself.

**The second is used for writing.** The code talks to a documented API, and the fragile
part — unofficial interaction with Garmin — is left to a service that maintains it
officially. The unofficial client stays necessary for **reading**, where intervals.icu
does not expose the same recovery metrics.

### 4.2 Components

Each component has one responsibility.

| Component | Responsibility |
|---|---|
| `garmin_reader` | Read Garmin Connect into an immutable `Snapshot` |
| `load` | Derive acute and chronic load from activities when the watch does not report it |
| `regime` | Decide how much of the history still counts (section 8) |
| `guardrails` | Validate a proposed week against the hard rules (section 7) |
| `plan_writer` | Parse and serialise `plan.md` |
| `intervals_pusher` | Reconcile the intervals.icu calendar with the planned week |
| `sheet_exporter` | Mirror `plan.md` into a Google Sheet, one way |
| `coach` (Claude skill) | Decide: run the intake, write and adapt the plan |

The separation that matters: **`guardrails` is deterministic code, not a prompt.** A
constraint verified by a language model is not a constraint.

---

## 5. Data — the files

| File | Lifetime | Content |
|---|---|---|
| `config/rules.toml` | in the repository | every tunable rule: regime thresholds, guardrails. Nothing about an athlete |
| `data/athlete.md` | written once, rarely updated | stable profile: injuries, constraints, access, preferences |
| `data/athlete.toml` | written at intake | the athlete's numbers the code reads: baseline floor, periods run without recording |
| `data/goal.md` | one per training cycle | goal, race, date, target, known commitments |
| `data/plan.md` | rewritten every week | the whole cycle, week by week, with the reason for each change |

**The athlete's files stay on their machine.** The two kinds of file live in two
folders: `config/` travels with the code, `data/` belongs to the athlete and is
git-ignored as a whole. The repository holds code, rules and the skill only, so cloning
it never hands one athlete's profile or plan to another.

The price is stated: there is no backup and no git history of the plan. The human veto
is therefore the comparison the skill shows before rewriting the plan; the current plan
survives on intervals.icu and on the Google Sheet. An athlete who wants history can keep
`data/` as a private repository of its own.

`plan.md` is the single source of truth. It holds every week up to the race: the coming
one in full detail, the later ones as a skeleton (one line per workout, no steps).

---

## 6. Intake and training load

The intake asks only what the data cannot say. **Phase 0** (once) covers injuries, fixed
weekly constraints, access to track or hills, dislikes, personal bests not on Garmin,
years of running, and periods run without recording. **Phase 1** (at every goal) covers
the race, its priority, the target, the long-run day and known commitments. The **weekly
check-in** reverses the rule and asks almost nothing: new pain, and anything that upends
the coming week. The exact protocol is in `.claude/skills/coach/SKILL.md`.

**Training load.** Performance watches report acute and chronic load; lifestyle models
(the Venu series among them) do not. When the watch does not report it, the load is
derived with Banister's TRIMP from what every run has, duration and average heart rate:

```
TRIMP = minutes × hrr × Y        hrr = (avg HR − resting HR) / (max HR − resting HR)
Y = 0.64 · e^(1.92·hrr)  for men        0.86 · e^(1.67·hrr)  for women
```

Acute load is the mean daily TRIMP over 7 days, chronic over 28. Resting heart rate,
maximum heart rate and sex are read from Garmin, not asked.

A window with no run has zero load: that is rest, and it is known. A window in which
**any** run cannot be scored — no heart rate — has an **unknown** load, not a lower one.
Leaving the run out would report less than the truth without saying so, and a week of
runs recorded without heart rate would read as a week off.

---

## 7. Guardrails

Rules kept apart from the plan and **checked by deterministic code before every
publication**.

### 7.1 Why they are the most important part of the design

A language model asked to "adapt the plan" every week, with no hard limits, sooner or
later produces something that causes an injury. Not through a gross error: through
plausibility. Each weekly decision looks reasonable, and the drift accumulates over
twelve weeks without anyone seeing it.

### 7.2 The rules

Every rule states what happens when the data it needs is missing (8.3).

| Rule | Nominal | When the data is missing |
|---|---|---|
| Empty week | refused | always computable |
| Quality sessions | at least one easy day between two | always computable |
| Down week | at least every 4 weeks, advisory | always computable |
| Weekly volume | +10% on the recent level flagged, see 7.4 | a day with no data counts as zero: the reference can only drop |
| Long run | +10% on the longest recent run flagged, +30% refused, see 7.6 | no run in the window: the absolute long-run cap applies |
| Long run as a share of the week | above 60% flagged, from three runs up | always computable |
| Acute/chronic load ratio | 0.8–1.3; above 1.3 flagged, above 1.7 refused | replaced by the absolute caps below |
| Absolute caps | weekly volume and long run, in kilometres | this **is** the fallback |

The acute/chronic ratio is a heuristic debated in the literature, not an established
result, and the cohort study behind 7.6 found no clear association between it and
injury. Its value here is not physiological precision: it is a limit the system can
check by itself.

### 7.3 Two severities

A violation either stops the week (**BLOCK**) or flags it (**warning**).

One gate for everything means a sensible week refused over two decimals, which teaches
the operator to bypass the check — and a guardrail bypassed by routine is worse than no
guardrail. A warning exists only in the band between the nominal threshold and the hard
one. A rule that is categorical, or that fires because data is missing, always blocks:
an advisory fail-closed fallback would be no fallback at all.

A blocking violation stops the publication. The system never corrects in silence: it
reports the violation and the plan has to be changed.

### 7.4 The reference for the volume ramp

Comparing each week only with the one before fails twice. It contradicts the down-week
rule — after a lighter week the reference is the lighter week, so returning to the
earlier level counts as a jump — and after a trip it locks the athlete out: two weeks at
5 km, and anything above 6 km is refused whatever they ran a month before.

| | |
|---|---|
| Reference | the larger of the previous week (the smaller of planned and actually run) and the **average of the last 4 weeks** |
| Clean | up to the reference +10% |
| Warning | beyond, up to the larger of reference +25% and the **highest of the last 4 weeks** |
| Block | beyond |

Returning to a level held recently is a choice to justify; exceeding everything recent
is not allowed. After more than four weeks off the earlier level leaves the window and
the climb starts over, consistently with 8.4. The first week of a plan follows the same
rule: with no previous week the reference is the average alone.

Each day counts what was run if the snapshot has reached it, what is planned if it is
still ahead, and zero otherwise.

### 7.5 The whole cycle, not only the coming week

`validate --all` checks the shape of every week of the plan with the structural rules
only: volume ramp, down weeks, long run, quality spacing. The rules that read the
athlete's state today — the load ratio and the absolute caps — apply only to the week
about to be published: today's load says nothing about a week four months away.

This serves two purposes: the athlete sees where the plan leads, and a plan that cannot
reach its goal inside the guardrails is discovered on day one rather than mid-cycle.

### 7.6 The long run is measured against the long run

A cap on the long run as a share of weekly volume is a rule of thumb sized for four to
six runs a week. With three runs every run is already a third of the total: 7 + 7 + 18 km
is 56%. And under three runs such a rule cannot say anything.

The rule in force compares the week's longest run with the **longest run of the previous
30 days**: clean up to +10%, warning up to +30%, block beyond. It applies to every week,
including those of one or two runs. A long run counts as recorded up to the snapshot and
as planned after it: one written in the plan and never run licenses nothing.

The bands come from a cohort of 5,205 Garmin users followed for 18 months (Br J Sports
Med, 2025): injury rate rises once a single session exceeds the longest run of the
previous 30 days by more than 10%, and the same study finds no clear association with
week-to-week change or with the acute/chronic ratio. This is the rule with evidence
behind it; the weekly rules stay as verifiable prudential limits.

The share survives as a warning above 60%. It never blocks, because the most lopsided
week of a plan is the legitimate one: a race week.

### 7.7 A week already under way

A plan written on a Wednesday holds only the days left. The volume judged is what was
already run that week plus what is still planned; today counts as the larger of run and
planned, since the session may not have happened yet. A workout must also sit under the
heading of the ISO week its date falls in: the heading decides which week it is judged
in, the date decides when it is run, and when they disagree the plan is rejected.

---

## 8. Cold start and insufficient data

"Garmin has no workouts yet" is not an edge case: it is the system's **first day**, and
every return from a break brings it back.

### 8.1 What breaks without data

The chronic load uses a 28-day window: under four weeks it is not reliable, so the load
ratio **cannot be computed**. Paces and volumes have nothing to be derived from.

### 8.2 Three data regimes

The regime is decided **before** generating or adapting any plan.

| Regime | Condition | Behaviour |
|---|---|---|
| **Cold** | under 14 days of usable history | The intake asks what the data cannot say: current volume, longest recent run, perceived easy pace. The plan starts in *bootstrap* mode. |
| **Warm** | 14–27 days | Volume and paces from real data. The load-based rule is **replaced** by the absolute caps. |
| **Hot** | 28 days or more | Full regime, every rule active. |

**Bootstrap mode:** two to four deliberately conservative weeks, flat volume, at most one
light quality session. It is what a coach who does not know the athlete would do: watch
before prescribing. The plan must **say** why its first weeks are cautious, or it reads
as a poor plan rather than a careful one.

### 8.3 Fail closed, never skip

**If a guardrail cannot be computed it is not switched off: it is replaced by a stricter
one.**

The mistake to avoid is `if chronic_load is None: skip_check()`. It would disable the
check at the moment of greatest vulnerability: a new athlete, or one coming back. For
the same reason `None` means missing and never zero — a load of zero is an unworn
device, not a rested athlete, and the two lead to opposite decisions.

### 8.4 The decay rule

The worst case is not missing data, it is a **hole** in the data. Three months off for an
injury, then two runs last week: the chronic load is near zero and the acute one is high,
or — reading the historical average — the system sees the volume of six months ago and
plans from there. That is the injury scenario.

**An interruption longer than a threshold (three weeks) invalidates the baseline and
returns the athlete to Cold, whatever came before.** A return is a beginning, not a
continuation. History is counted from the first run *after* the last long interruption,
so one old layoff does not pin a now-consistent athlete to Cold for ever.

### 8.5 Young data, experienced athlete

The mirror image of 8.4: there the training stopped, here the **data** stopped while
training went on. A broken or replaced watch.

To Garmin the two cases are identical, and treating them the same way is wrong half the
time. The error here is underestimation: the system treats the athlete as a returning
beginner and the plan comes out too easy — not an injury risk, a risk of being dropped
out of boredom.

Only the athlete knows which case it is, so they declare it in `athlete.toml`:

- **data gaps** — periods with no recordings in which training continued. A declared gap
  is subtracted before judging whether a hole is a real interruption. Dates can be
  approximate.
- **a baseline floor** — a minimum weekly volume and long run below which the absolute
  caps do not drop. It can loosen a cap but never in the Cold regime, where the history
  it is built from has been invalidated.

---

## 9. The weekly run

Once a week, started by the athlete on their own computer:

1. Read the snapshot and decide the **regime** (8.2). It is the first decision of the
   run, because it determines which rules apply.
2. Compare planned with done.
3. Ask the two check-in questions.
4. Claude proposes the coming week and, where reality moved, the skeleton after it,
   justifying every deviation.
5. Show the athlete what changes; on their go-ahead, write `plan.md`.
6. `validate --all` checks the proposal. A blocking violation stops here.
7. `export` mirrors the plan to the Google Sheet.
8. `push` publishes **the coming week only** to intervals.icu.

One week is published at a time: a plan already on the watch for twelve weeks is a plan
that does not adapt.

**Publishing reconciles, it does not just add.** Each published session carries a stable
external id — its day and its position within that day. If it changes it is updated in
place; if the plan no longer holds it, it is removed. Only events this program created
are touched, and nothing dated before today is rewritten.

**The sheet is one-way.** It is overwritten at every export and never read back: a
second editable copy would diverge from the plan at the first adapted week.

---

## 10. Status

What exists: the intake, plan generation, the guardrails, manual publication and the
sheet export. Every run is started by hand.

What does not exist yet: a scheduled weekly run, and a per-week ledger of planned
against done with the decision taken — the record that would make it possible to judge
the *coach* rather than the athlete.

The quality of training decisions cannot be verified automatically, and this design does
not pretend otherwise: right and wrong choices look alike, and the difference shows
after weeks. That is why automation comes after the plan has convinced a person, not
before.

---

## 11. Error handling

Principle: **fail loudly at the boundary, never degrade in silence.** Each expected
failure stops the run with a message that names it; anything else is a bug and must
look like one, so there is no catch-all.

| Situation | Behaviour |
|---|---|
| Garmin login fails, or a verification code is needed | stop. Never a plan on stale data without saying so |
| Garmin fails after the login (rate limit, timeout, changed payload) | stop: a snapshot is all or nothing |
| A metric is missing | the run continues, and the rule that needs it uses its fallback (8.3) |
| A setting is missing from `.env` | stop, naming the variable |
| `rules.toml` or `athlete.toml` is malformed | stop, naming the file and the section |
| `plan.md` is malformed | stop, with the line number. A line visible in the plan and ignored by the code would break the human veto |
| A guardrail blocks | stop, the violation is reported, nothing is published |
| intervals.icu is unreachable, or fails midway | the plan stays written; what did land is reported, and running `push` again completes it |

---

## 12. Testing

- `guardrails` deserves the most tests and is the easiest to test: a pure function from
  a week and a snapshot to a list of violations. Every fallback has a test that verifies
  the constraint becomes **stricter**, not absent.
- `garmin_reader` is tested against recorded payloads, never against the network.
- `intervals_pusher` and `sheet_exporter` are tested against a simulated API. Check the
  first real publication on the watch.
- The suite is hermetic: no network, no credentials, none of the athlete's files.

---

## 13. Sources

- Strava API Policy — https://www.strava.com/legal/api_policy
- `cyberjunky/python-garminconnect` — https://github.com/cyberjunky/python-garminconnect
- intervals.icu API — https://intervals.icu/api/v1/docs
- intervals.icu, uploading planned workouts — https://forum.intervals.icu/t/uploading-planned-workouts-to-intervals-icu/63624
- "How much running is too much?", British Journal of Sports Medicine, 2025 — https://pmc.ncbi.nlm.nih.gov/articles/PMC12421110
