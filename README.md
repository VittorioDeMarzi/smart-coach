# smart-coach

A personal running coach that lives on your computer. It reads your data from Garmin,
asks you a few questions, writes a plan up to race day and publishes the workouts to
your watch, already structured (warm-up, repeats, pace). Every week it looks at what you
actually ran and adapts the rest.

It is not an app and has no interface: you use it by talking to Claude Code in the
terminal. It costs 0 EUR a month on top of the Claude subscription you already have.

> This is a personal tool, not medical advice. The safety rules limit the most common
> mistakes; they do not replace common sense. If something hurts, stop.

## How it works

```
Garmin Connect ──read──▶ smart-coach ──plan──▶ intervals.icu ──sync──▶ Garmin watch
                              │
                              ├──▶ data/plan.md      the plan, on your computer
                              └──▶ Google Sheet      a copy to look at (optional)
```

- **Claude** decides what you should do: it runs the initial questions and writes the
  plan.
- **The code** decides whether that plan is admissible: fixed rules on volume, long run
  and recovery. A plan that breaks them is not published, whatever Claude says.
- **intervals.icu** is the bridge to the watch, because Garmin offers no official way to
  upload workouts from a personal program.

## What you need

| | |
|---|---|
| A Garmin watch and a Garmin Connect account | with a few weeks of recorded runs, if possible |
| [Claude Code](https://claude.com/claude-code) with a Claude subscription | it is the coach's interface |
| An [intervals.icu](https://intervals.icu) account | free |
| Python 3.12 or newer, and git | |
| A computer on a home network | Garmin blocks connections from cloud servers and from many VPNs |
| A Google account | only if you also want the plan on a Google Sheet |

## Installation

Time: 20-30 minutes the first time.

### 1. Download and install

```bash
git clone https://github.com/VittorioDeMarzi/smart-coach.git
cd smart-coach
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Check that everything is in place:

```bash
.venv/bin/python -m pytest
```

Every test must pass. They touch neither the network nor your accounts.

### 2. Create the credentials file

```bash
cp .env.example .env
```

`.env` stays on your computer only: git ignores it. Open it and fill in the first two
lines with your Garmin Connect email and password:

```
GARMIN_EMAIL=...
GARMIN_PASSWORD=...
```

The password is needed for the first login; after that the session is kept in
`~/.garminconnect`.

### 3. Connect intervals.icu

1. Create an account on [intervals.icu](https://intervals.icu).
2. Go to **Settings** and find the **Garmin Connect** section: connect your Garmin
   account and **tick the option to upload planned workouts**. Without that tick the
   workouts reach intervals.icu but not the watch.
3. Still in Settings, open the **Run** tab and, under the pace settings, fill in the
   **Threshold pace**: the pace you could hold for about an hour (a recent 10K pace is a
   fair start). Leave the button that applies the change to past activities alone.
   Without a threshold pace, any target written as a zone or a percentage reaches the
   watch **with no pace at all**, and nothing tells you.
4. Still in Settings, near the bottom, find **Developer Settings**. There you have:
   - your **Athlete ID**, which starts with `i` (for example `i123456`): the `i` is part
     of the ID and must be copied;
   - the **API key**: if there is none, generate it.
5. Copy both into `.env`:

```
INTERVALS_ATHLETE_ID=i123456
INTERVALS_API_KEY=...
```

The API key is as good as a password: do not paste it into a chat or a message.

### 4. First login to Garmin

Run this command **in a real terminal**, not inside Claude:

```bash
.venv/bin/python -m smart_coach.cli snapshot
```

If your account has two-step verification, Garmin sends you a code and the command asks
for it. It happens once.

If it works you see your data regime, your load and the runs of the last 28 days with
pace, heart rate, climb and place. Check that they really are your runs.

### 5. Google Sheet (optional)

It lets you have the plan on your phone without opening the computer. The sheet is a
one-way copy: the system rewrites it, and edits made by hand are lost.

1. On [console.cloud.google.com](https://console.cloud.google.com) create a project and
   enable the **Google Sheets API** (APIs & Services → Library).
2. Under **Credentials** create a **service account**: a name is enough, then "Create
   and close".
3. Open the service account, **Keys** tab → Add key → JSON. A file is downloaded: move
   it out of the repository.

```bash
mkdir -p ~/.config/smart-coach
mv ~/Downloads/<downloaded-file>.json ~/.config/smart-coach/google-service-account.json
chmod 600 ~/.config/smart-coach/google-service-account.json
```

4. Create an empty Google Sheet and **share it as Editor** with the service account's
   email (it ends in `.iam.gserviceaccount.com`).
5. Add to `.env` the sheet ID (the part of the URL between `/d/` and `/edit`) and the
   path to the key:

```
GOOGLE_SHEET_ID=...
GOOGLE_SERVICE_ACCOUNT_FILE=~/.config/smart-coach/google-service-account.json
```

6. Try the connection:

```bash
.venv/bin/python -m smart_coach.cli export
```

Until a plan exists it answers that the plan is missing: that is normal. After the first
plan it writes the rows to the sheet.

## The first plan

Open Claude Code in the project folder and type:

```
/coach
```

The coach asks two rounds of questions, one at a time, in your language:

- **Who you are as a runner** (once): injuries, available days, other sports, access to
  a track or hills, periods in which you ran without recording.
- **What you want to do** (at every goal): the race, its date, the time you have in
  mind, commitments already in the calendar such as holidays or trips.

It does not ask what Garmin already knows: volume, paces, heart rate.

Then it writes the plan in `data/plan.md`: every week up to the race, with the steps in
detail for the coming week and a skeleton for the others. It checks it against the rules
and shows it to you. **Read it and push back on whatever does not convince you**: too
easy, wrong day, a session you would not do. The coach rewrites it.

## Publishing to the watch

When the week convinces you:

```bash
.venv/bin/python -m smart_coach.cli push
```

It publishes **the coming week only**, never the whole plan, so the rest can still
change. For a specific week, for example the one in progress:

```bash
.venv/bin/python -m smart_coach.cli push --week 42
```

Before publishing, the command checks the rules again. Two possible outcomes:

- **`BLOCK`**: the week is not published. It has to be fixed in the plan.
- **`warning`**: it is published. It means "look here", not "no".

Then open intervals.icu: the workouts are in the calendar. From there they reach Garmin
Connect and the watch at the next sync; it can take a few minutes. **The first time,
check one workout on the watch**: that it has the right steps and a sensible pace.

If you change a week that was already published, run `push` again: it updates the
workouts you changed and removes the ones taken out of the plan. It only touches events
created by smart-coach, and never days already gone.

## Every week

On Sunday, or whenever you want to review the plan:

1. Open Claude Code and type `/coach`.
2. The coach reads what you ran, asks only whether there is new pain or anything
   unexpected, and adapts the coming week and the skeleton after it.
3. Before rewriting the plan it shows you what changes. You give the go-ahead.
4. Publish with `push`.

## Where your data lives

Everything about you stays on your computer. Git ignores it, so it cannot end up on
GitHub even by mistake.

| File | Content |
|---|---|
| `.env` | your credentials |
| `data/athlete.md` | your profile: injuries, constraints, preferences |
| `data/athlete.toml` | your numbers: periods run without recording, presumed minimum volume |
| `data/goal.md` | the goal of the cycle |
| `data/plan.md` | the plan |

The consequence: **there is no backup**. If you lose the computer, the current plan
survives on intervals.icu and on the Google Sheet, and the profile is rebuilt by
answering the questions again. If you want history and a backup, create a private git
repository inside `data/`.

The whole `data/` folder is yours and nothing else lives there. The rules, the same for
everyone, are in `config/rules.toml`, which is in the repository: you can adjust the
thresholds, and each one has its explanation next to it.

## Commands

| Command | What it does |
|---|---|
| `snapshot` | shows what Garmin knows about you right now |
| `validate` | checks the coming week against the rules |
| `validate --week 42` | checks a specific week |
| `validate --all` | also checks the shape of every later week |
| `push` | publishes the coming week to intervals.icu |
| `push --week 42 --year 2027` | publishes a specific week |
| `export` | copies the plan to the Google Sheet |

All of them are preceded by `.venv/bin/python -m smart_coach.cli`.

## Messages you may meet

| Message | What it means | What to do |
|---|---|---|
| `Missing environment variable: ...` | that line is missing from `.env` | add it |
| `Garmin asks for a verification code` | first login, or the session expired | run `snapshot` in a real terminal |
| `Garmin login failed` | wrong email or password; if the text mentions 403 or 429, it is the network | check `.env`; in the second case turn the VPN off and use your home network |
| `Garmin read failed ... 429` | Garmin is rate-limiting you | wait an hour, do not insist |
| `data/plan.md does not exist yet` | there is no plan yet | `/coach` |
| `Week ... is not in the plan` | the plan does not hold that week | name the week with `--week`, or have the plan updated |
| `plan.md line N: ...` | a line of the plan is malformed | fix that line, or ask the coach |
| `Load data unavailable` | a recent run has no heart rate, or the history is too short | stricter limits apply until the data is back |
| `Access to the sheet denied` | the sheet is not shared with the service account | share it as Editor |
| `...: missing section [...]` | `config/rules.toml` or `data/athlete.toml` is incomplete | restore the section named |

## Known limits

- **Running only.** Gym, cycling and other sports enter as constraints, not into the
  plan.
- **Unofficial access to Garmin.** Garmin has no public API for personal use: reading
  goes through a community library and can break if Garmin changes something. Your
  password sits in plain text in `.env`, on your computer.
- **No Strava data**, by design: Strava's API policy forbids using its data with AI.
- **One athlete per computer**, normally. For two people under the same user, each one
  adds a `GARMIN_TOKEN_STORE=` line to `.env` with a different folder: otherwise the
  second would read the first one's runs without noticing.
- **Watches without training load** (the Venu series, for example): the load is derived
  from the duration and heart rate of each run.

## For those who want to change it

- `CLAUDE.md`: project constraints and conventions.
- `docs/design.md`: how the system is built and the reason behind every rule.
- `.claude/skills/coach/SKILL.md`: the protocol the coach follows.
- `src/smart_coach/guardrails.py`: the safety rules. They are code, not prompts: a rule
  verified by a model is not a rule.

```bash
.venv/bin/python -m pytest
```


## Licence

[MIT](LICENSE): you may use, copy and modify it. It is provided as is, with no warranty.
