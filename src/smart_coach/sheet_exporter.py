"""Mirror plan.md into a Google Sheet, one way.

The sheet is a view for reading away from the repository. plan.md stays the only source
of truth: every export overwrites the sheet and nothing is ever read back from it, so
an edit made there cannot drift into the plan.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol

import gspread
from gspread.exceptions import GSpreadException

from smart_coach.models import Plan, Week, Workout

HEADER = ("Week", "Date", "Day", "Session", "Km", "Type", "Week km", "Details")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
NO_RUNNING = "— no running planned —"
STEP_SEPARATOR = " · "
STEP_BULLET = "- "
# Sheets only. The spreadsheet is opened by id, which needs no Drive access.
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
# Dates and numbers are parsed as the athlete would have typed them, not stored as text.
INPUT_AS_TYPED = "USER_ENTERED"


class SheetExportError(RuntimeError):
    """The sheet could not be reached or written. Carries what to fix."""


class Worksheet(Protocol):
    def clear(self) -> Any: ...
    def update(self, values: list[list[Any]], range_name: str,
               value_input_option: str) -> Any: ...
    def freeze(self, rows: int) -> Any: ...


def _label(iso_year: int, iso_week: int) -> str:
    return f"{iso_year}-W{iso_week:02d}"


def _detail(workout: Workout) -> str:
    return STEP_SEPARATOR.join(step.text.removeprefix(STEP_BULLET)
                               for step in workout.steps)


def _week_rows(week: Week) -> list[list[Any]]:
    """One row per workout. The weekly total sits on the first row only, so the column
    can be summed without counting a week once per workout."""
    return [
        [_label(week.iso_year, week.iso_week), workout.day.isoformat(),
         WEEKDAYS[workout.day.weekday()], workout.name, workout.total_km,
         "quality" if workout.is_quality else "easy",
         week.total_km if position == 0 else "", _detail(workout)]
        for position, workout in enumerate(week.workouts)
    ]


def _iso_weeks(first: Week, last: Week) -> Iterator[tuple[int, int]]:
    """Every ISO week from `first` to `last`, stepping by date: ISO years have 52 or 53
    weeks, so the week number cannot simply be incremented."""
    monday = date.fromisocalendar(first.iso_year, first.iso_week, 1)
    end = date.fromisocalendar(last.iso_year, last.iso_week, 1)
    while monday <= end:
        yield monday.isocalendar()[:2]
        monday += timedelta(weeks=1)


def plan_rows(plan: Plan) -> list[list[Any]]:
    """The whole plan as sheet rows. A week the plan skips gets an explicit row: in a
    reference view a missing week reads as a mistake."""
    rows: list[list[Any]] = [list(HEADER)]
    if not plan.weeks:
        return rows
    planned = {(week.iso_year, week.iso_week): week for week in plan.weeks}
    for key in _iso_weeks(plan.weeks[0], plan.weeks[-1]):
        if key in planned:
            rows += _week_rows(planned[key])
        else:
            rows.append([_label(*key), "", "", NO_RUNNING, "", "", 0.0, ""])
    return rows


def write_rows(worksheet: Worksheet, rows: list[list[Any]]) -> None:
    """Replace the sheet's values. Cleared first, so a plan that got shorter leaves no
    stale rows below it; formatting applied by hand in the sheet is not touched."""
    worksheet.clear()
    worksheet.update(rows, "A1", value_input_option=INPUT_AS_TYPED)
    worksheet.freeze(rows=1)


def open_worksheet(sheet_id: str, key_file: str) -> Worksheet:
    """First tab of the spreadsheet, authenticated as the service account."""
    key_file = str(Path(key_file).expanduser())  # .env is not a shell: `~` is literal
    try:
        client = gspread.service_account(filename=key_file, scopes=SCOPES)
        return client.open_by_key(sheet_id).sheet1
    except FileNotFoundError as missing:
        raise SheetExportError(
            f"Service account key not found: {key_file}") from missing
    except PermissionError as denied:
        raise SheetExportError(
            "Access to the sheet denied: share it with the service account's email, "
            "as Editor.") from denied
    except GSpreadException as failure:
        raise SheetExportError(f"Google Sheets rejected the request: {failure}") \
            from failure


def export_plan(plan: Plan, sheet_id: str, key_file: str) -> int:
    """Write the plan to the sheet. Returns the number of data rows written."""
    rows = plan_rows(plan)
    worksheet = open_worksheet(sheet_id, key_file)
    try:
        write_rows(worksheet, rows)
    except GSpreadException as failure:
        raise SheetExportError(f"Writing to the sheet failed: {failure}") from failure
    return len(rows) - 1
