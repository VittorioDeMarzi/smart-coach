"""Command line entry points. Every run in M1 is started by hand."""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from smart_coach.config import (Config, ConfigError, MissingSettingError, load_config,
                                required_setting)
from smart_coach.garmin_reader import (GarminAuthError, GarminError, build_client,
                                       fetch_snapshot)
from smart_coach.guardrails import blocking, validate, validate_structure
from smart_coach.intervals_pusher import PushResult, push_week
from smart_coach.models import (Activity, Plan, Regime, Severity, Snapshot, Week,
                                next_iso_week)
from smart_coach.plan_writer import (PlanNotFoundError, PlanParseError, read_plan,
                                     weeks_since_down_week)
from smart_coach.regime import determine_regime
from smart_coach.sheet_exporter import SheetExportError, export_plan

DEFAULT_CONFIG = "config/rules.toml"
# The athlete's own numbers. Never in the repository; absent for a new athlete.
DEFAULT_ATHLETE = "data/athlete.toml"
ENV_FILE = ".env"
DEFAULT_PLAN = "data/plan.md"
SNAPSHOT_DAYS = 28
# The failures a run is expected to meet: each is reported as a stop with a notice
# (spec 11), never as a traceback. Anything else is a bug and must look like one, so
# there is deliberately no catch-all here.
STOPS = (ConfigError, MissingSettingError, PlanNotFoundError, PlanParseError,
         GarminError, SheetExportError)


def _config(rules_path: str) -> Config:
    """The rules from `rules_path` together with this machine's athlete numbers."""
    return load_config(rules_path, DEFAULT_ATHLETE)


def mfa_prompt() -> str:
    """Ask the athlete for Garmin's one-time code.

    Only ever from a real terminal: the code is theirs to type, and a run with no
    terminal must fail with an instruction rather than hang waiting on stdin.
    """
    if not sys.stdin.isatty():
        raise GarminAuthError(
            "Garmin asks for a verification code. Run this command in your own "
            "terminal, once: after the first login the token stays cached and later "
            "runs do not ask again."
        )
    return input("Garmin verification code: ").strip()


def _report(violations) -> None:
    for violation in violations:
        mark = "BLOCK   " if violation.severity is Severity.BLOCK else "warning "
        print(f"  {mark}[{violation.rule}] {violation.message}")


def validate_command(
    week: Week, previous_week: Week | None, snapshot: Snapshot, regime: Regime,
    config_path: str = DEFAULT_CONFIG, weeks_since_down: int = 0,
    planned_weeks: tuple[Week, ...] = (),
) -> int:
    violations = validate(week, previous_week, snapshot, regime, _config(config_path),
                          weeks_since_down_week=weeks_since_down,
                          planned_weeks=planned_weeks)
    blockers = blocking(violations)
    if violations:
        print(f"Week {week.iso_week} (regime {regime.value}):")
        _report(violations)
    if blockers:
        return 1
    print(f"OK — week {week.iso_week} is admissible (regime {regime.value})."
          + (" Warnings above: read them, they do not block." if violations else ""))
    return 0


def push_command(
    week: Week, previous_week: Week | None, snapshot: Snapshot, regime: Regime,
    athlete_id: str, api_key: str, config_path: str = DEFAULT_CONFIG,
    weeks_since_down: int = 0, planned_weeks: tuple[Week, ...] = (),
) -> int:
    if validate_command(week, previous_week, snapshot, regime, config_path,
                        weeks_since_down, planned_weeks) != 0:
        print("Push refused: fix the blocks listed above in the plan.")
        return 1
    result = push_week(week, athlete_id, api_key,
                       _config(config_path).workout.easy_step_target,
                       today=snapshot.as_of)
    print(f"Calendar: {result.created} created, {result.updated} updated, "
          f"{result.removed} removed, {result.skipped} unchanged.")
    if result.error:
        print(f"Push interrupted: {result.error}. The plan is still written: retry.")
        return 1
    return 0


def previous_week_of(plan: Plan, index: int) -> Week | None:
    """The plan entry for the ISO week right before, or None when the plan skips it.

    A week with no running has no heading, so the entry above in the file can be weeks
    older; treating it as last week would measure the ramp from a level long gone.
    """
    if index == 0:
        return None
    week, candidate = plan.weeks[index], plan.weeks[index - 1]
    monday = date.fromisocalendar(week.iso_year, week.iso_week, 1)
    expected = (monday - timedelta(weeks=1)).isocalendar()[:2]
    return candidate if (candidate.iso_year, candidate.iso_week) == expected else None


def outlook_command(
    plan: Plan, first_index: int, snapshot: Snapshot, config_path: str = DEFAULT_CONFIG
) -> int:
    """Check the shape of every week from `first_index` on.

    Only the structural rules: the athlete's load today says nothing about a week
    months ahead, and those weeks are rewritten before they are published anyway.
    """
    config = _config(config_path)
    blocked = 0
    for index in range(first_index, len(plan.weeks)):
        week = plan.weeks[index]
        violations = validate_structure(
            week, previous_week_of(plan, index), snapshot, config,
            weeks_since_down_week(plan.weeks, index), plan.weeks)
        if violations:
            print(f"Week {week.iso_year}-W{week.iso_week:02d}:")
            _report(violations)
        blocked += bool(blocking(violations))
    checked = len(plan.weeks) - first_index
    print(f"Rest of the cycle: {checked} weeks checked, {blocked} with blocks.")
    return 1 if blocked else 0


def select_week_index(
    plan: Plan, week_number: int | None, iso_year: int | None, today: date
) -> int:
    """Index of the week to act on.

    With no explicit week the target is the ISO week after `today`, never the first entry
    in the file: spec 9 publishes only the next week. The match includes the ISO year,
    because a plan spanning a year boundary can hold two weeks with the same number.
    """
    if week_number is None:
        target = next_iso_week(today)
    else:
        target = (iso_year or today.isocalendar()[0], week_number)
    index = next((i for i, w in enumerate(plan.weeks)
                  if (w.iso_year, w.iso_week) == target), None)
    if index is None:
        raise SystemExit(f"Week {target[0]}-W{target[1]:02d} is not in the plan.")
    return index


def _load_state(plan_path: str, week_number: int | None, iso_year: int | None):
    plan = read_plan(plan_path)
    index = select_week_index(plan, week_number, iso_year, date.today())
    snapshot = fetch_snapshot(build_client(mfa_prompt), date.today(), SNAPSHOT_DAYS)
    regime = determine_regime(snapshot, _config(DEFAULT_CONFIG))
    return plan, index, snapshot, regime


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smart-coach")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("export")
    for name in ("validate", "push"):
        command = sub.add_parser(name)
        command.add_argument("--week", type=int, default=None)
        command.add_argument("--year", type=int, default=None)
    sub.choices["validate"].add_argument(
        "--all", action="store_true",
        help="also check the shape of every later week of the cycle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(ENV_FILE)  # credentials live in .env, which is git-ignored

    try:
        return _run(args)
    except STOPS as failure:
        print(failure, file=sys.stderr)
        return 1


def _pace(activity: Activity) -> str:
    if not activity.distance_km:
        return "?:??"
    minutes, seconds = divmod(round(activity.duration_s / activity.distance_km), 60)
    return f"{minutes}:{seconds:02d}"


def _or_unknown(value) -> str:
    return "?" if value is None else f"{value:.0f}"


def recent_runs_report(snapshot: Snapshot) -> list[str]:
    """One line per run in the load window, with the context a pace needs to be read:
    a missing value prints as `?`, never as zero."""
    return [
        f"{run.day}  {run.distance_km:4.1f} km  {_pace(run)}/km  "
        f"HR {_or_unknown(run.avg_hr):>3}  climb {_or_unknown(run.elevation_gain_m):>3} m  "
        f"{run.location or '?'}"
        for run in snapshot.recent_runs
    ]


def _snapshot_command() -> int:
    snapshot = fetch_snapshot(build_client(mfa_prompt), date.today(), SNAPSHOT_DAYS)
    regime = determine_regime(snapshot, _config(DEFAULT_CONFIG))
    print(f"regime: {regime.value} | days of data: {snapshot.days_of_data} | "
          f"running km: {snapshot.running_km:.1f}")
    print(f"chronic/acute load: {snapshot.chronic_load}/{snapshot.acute_load} | "
          f"missing: {snapshot.missing_metrics or 'none'}")
    print(f"VO2max: {snapshot.vo2max} | resting HR: {snapshot.resting_hr} | "
          f"HRV: {snapshot.hrv_status}")
    print(f"Runs of the last {SNAPSHOT_DAYS} days:")
    print("\n".join(recent_runs_report(snapshot)))
    return 0


def _export_command() -> int:
    """Needs the plan and nothing else: no Garmin login, so it runs from anywhere."""
    written = export_plan(read_plan(DEFAULT_PLAN), required_setting("GOOGLE_SHEET_ID"),
                          required_setting("GOOGLE_SERVICE_ACCOUNT_FILE"))
    print(f"Sheet updated: {written} rows.")
    return 0


def _run(args) -> int:
    """Dispatch, inside the caller's error handling so every command is covered."""
    if args.command == "snapshot":
        return _snapshot_command()
    if args.command == "export":
        return _export_command()
    plan, index, snapshot, regime = _load_state(DEFAULT_PLAN, args.week, args.year)
    target = dict(weeks_since_down=weeks_since_down_week(plan.weeks, index),
                  planned_weeks=plan.weeks)
    week, previous = plan.weeks[index], previous_week_of(plan, index)
    if args.command == "push":
        return push_command(week, previous, snapshot, regime,
                            required_setting("INTERVALS_ATHLETE_ID"),
                            required_setting("INTERVALS_API_KEY"), **target)
    status = validate_command(week, previous, snapshot, regime, **target)
    if args.all:
        status = max(status, outlook_command(plan, index + 1, snapshot))
    return status


if __name__ == "__main__":
    sys.exit(main())
