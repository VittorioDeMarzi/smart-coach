"""Read and write plan.md.

The file is meant to be edited by hand, so parsing fails loudly with a line number
rather than accepting half a plan.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

from smart_coach.models import Plan, Step, Week, Workout

TITLE = "# Plan"
GOAL_PREFIX = "**Goal:**"
WEEK_HEADING = re.compile(r"^##\s+(\d{4})-W(\d{1,2})\s*$")
# Any line that opens with a date is an attempt at a workout, whatever bullet it uses
# (or none). Recognising the attempt is what lets us reject it loudly instead of
# dropping it: a line visible in the diff and ignored by the code breaks the human veto.
WORKOUT_ATTEMPT = re.compile(r"^\s*[-*]?\s*\d{4}-\d{2}-\d{2}")
WORKOUT_LINE = re.compile(
    r"^\s*[-*]?\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*\|\s*([\d.]+)\s*km"
    r"\s*\|\s*(easy|quality)\s*$"
)
# A step is any indented line under a workout. Two spaces is depth 0, four is depth 1
# (the body of a repeat block); intervals.icu does not support deeper nesting.
STEP_INDENT = 2
STEP_LINE = re.compile(r"^(\s{2,})(\S.*)$")


class PlanParseError(ValueError):
    def __init__(self, message: str, line_number: int) -> None:
        super().__init__(f"plan.md line {line_number}: {message}")
        self.line_number = line_number


class PlanNotFoundError(FileNotFoundError):
    """There is no plan yet. The athlete's files are not in the repository, so this is
    what the first command on a fresh clone meets: an instruction, not a crash."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} does not exist yet: create the plan with /coach.")


def _plan_lines(path: str | Path) -> list[str]:
    plan_path = Path(path)
    if not plan_path.exists():
        raise PlanNotFoundError(plan_path)
    return plan_path.read_text().splitlines()


def _parse_workout(line: str, line_number: int) -> Workout:
    match = WORKOUT_LINE.match(line)
    if match is None:
        raise PlanParseError(f"unrecognised workout line: {line.strip()!r}", line_number)
    day_text, name, km_text, kind = match.groups()
    return Workout(
        day=date.fromisoformat(day_text), name=name, sport="running",
        steps=(), total_km=float(km_text), is_quality=(kind == "quality"),
    )


def _in_week(workout: Workout, week: tuple[int, int], line_number: int) -> Workout:
    """The heading decides which week a session is judged in, its date decides when it
    is run. When the two disagree one of them is wrong, and guessing which hides it."""
    if workout.day.isocalendar()[:2] != week:
        raise PlanParseError(
            f"{workout.day} is not in week {week[0]}-W{week[1]:02d}", line_number)
    return workout


def _parse_step(indent: str, body: str) -> Step:
    return Step(text=body, depth=(len(indent) - STEP_INDENT) // STEP_INDENT)


def _with_step(workout: Workout, step: Step) -> Workout:
    return replace(workout, steps=workout.steps + (step,))


def read_plan(path: str | Path) -> Plan:
    goal: str | None = None
    weeks: list[Week] = []
    pending: list[Workout] = []
    current: tuple[int, int] | None = None

    def close_week() -> None:
        if current is not None:
            weeks.append(Week(iso_year=current[0], iso_week=current[1],
                              workouts=tuple(pending)))

    for number, line in enumerate(_plan_lines(path), start=1):
        if line.startswith(GOAL_PREFIX):
            goal = line[len(GOAL_PREFIX):].strip()
            continue
        if line.startswith("##"):
            heading = WEEK_HEADING.match(line)
            if heading is None:
                raise PlanParseError(
                    f"unrecognised week heading: {line.strip()!r}", number)
            close_week()
            pending = []
            current = (int(heading.group(1)), int(heading.group(2)))
            continue
        if WORKOUT_ATTEMPT.match(line):
            if current is None:
                raise PlanParseError("workout before any week heading", number)
            pending.append(_in_week(_parse_workout(line, number), current, number))
            continue
        step = STEP_LINE.match(line)
        if step:
            if not pending:
                raise PlanParseError(
                    f"step before any workout: {line.strip()!r}", number)
            pending[-1] = _with_step(pending[-1], _parse_step(*step.groups()))

    close_week()
    if goal is None:
        raise PlanParseError(f"missing '{GOAL_PREFIX}' line", 1)
    return Plan(goal=goal, weeks=tuple(weeks))


def weeks_since_down_week(weeks: tuple[Week, ...], index: int) -> int:
    """Weeks elapsed since the last reduction in volume, counting back from `index`.

    Returns `index` when no down week exists yet, so an unbroken build block makes the
    down-week guardrail fire rather than stay silent.
    """
    for offset in range(index - 1, 0, -1):
        if weeks[offset].total_km < weeks[offset - 1].total_km:
            return index - offset
    return index


def write_plan(plan: Plan, path: str | Path) -> None:
    """Serialise a Plan back to plan.md.

    Kept alongside `read_plan` as the executable definition of the accepted format: the
    round-trip test is what stops the writer and the parser drifting apart. Distances
    use `:g` so a fractional value survives the round trip unrounded.
    """
    lines = [TITLE, "", f"{GOAL_PREFIX} {plan.goal}", ""]
    for week in plan.weeks:
        lines += [f"## {week.iso_year}-W{week.iso_week:02d}", ""]
        for workout in week.workouts:
            kind = "quality" if workout.is_quality else "easy"
            lines.append(f"- {workout.day.isoformat()} | {workout.name} | "
                         f"{workout.total_km:g} km | {kind}")
            for step in workout.steps:
                lines.append(" " * (STEP_INDENT * (step.depth + 1)) + step.text)
        lines.append("")
    Path(path).write_text("\n".join(lines))
