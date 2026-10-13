"""The Google Sheet is a one-way mirror of plan.md for reading on a phone.

plan.md stays the only source of truth: the sheet is overwritten on every export and
never read back, so an edit made there cannot drift into the plan.
"""

from datetime import date

import pytest

from smart_coach import cli
from smart_coach.models import Plan, Step, Week, Workout
from smart_coach.sheet_exporter import (HEADER, NO_RUNNING, SheetExportError,
                                        open_worksheet, plan_rows, write_rows)


def _workout(day: date, name: str, km: float, quality: bool = False, steps=()) -> Workout:
    return Workout(day=day, name=name, sport="running", steps=steps, total_km=km,
                   is_quality=quality)


W42 = Week(iso_year=2026, iso_week=42, workouts=(
    _workout(date(2026, 10, 13), "Easy + strides", 7.0, steps=(
        Step(text="- 5km 5:50/km Pace"),
        Step(text="6x"),
        Step(text="- 20s 4:30/km Pace", depth=1),
        Step(text="- 60s 6:30/km Pace", depth=1),
    )),
    _workout(date(2026, 10, 15), "Intervals", 8.0, quality=True),
    _workout(date(2026, 10, 18), "Long run", 12.5),
))


def test_one_row_per_workout_under_a_header():
    rows = plan_rows(Plan(goal="Berlin Half", weeks=(W42,)))
    assert rows[0] == list(HEADER)
    assert rows[1] == ["2026-W42", "2026-10-13", "Tue", "Easy + strides", 7.0, "easy",
                       27.5, "5km 5:50/km Pace · 6x · 20s 4:30/km Pace · 60s 6:30/km Pace"]
    assert rows[2] == ["2026-W42", "2026-10-15", "Thu", "Intervals", 8.0, "quality", "", ""]
    assert rows[3] == ["2026-W42", "2026-10-18", "Sun", "Long run", 12.5, "easy", "", ""]


def test_the_weekly_total_appears_once_so_the_column_can_be_summed():
    totals = [row[6] for row in plan_rows(Plan(goal="x", weeks=(W42,)))[1:]]
    assert totals == [27.5, "", ""]


def test_a_week_the_plan_skips_is_shown_rather_than_silently_missing():
    """A fortnight away has no heading in plan.md. In the reference view a missing
    row reads as a mistake; an explicit one reads as a decision."""
    w51 = Week(iso_year=2026, iso_week=51, workouts=(
        _workout(date(2026, 12, 15), "Easy", 8.0),))
    w02 = Week(iso_year=2027, iso_week=2, workouts=(
        _workout(date(2027, 1, 12), "Easy", 6.0),))
    labels = [(row[0], row[3]) for row in plan_rows(Plan(goal="x", weeks=(w51, w02)))[1:]]
    assert labels == [("2026-W51", "Easy"), ("2026-W52", NO_RUNNING),
                      ("2026-W53", NO_RUNNING), ("2027-W01", NO_RUNNING),
                      ("2027-W02", "Easy")]


def test_an_empty_plan_exports_only_the_header():
    assert plan_rows(Plan(goal="x", weeks=())) == [list(HEADER)]


class FakeWorksheet:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def clear(self) -> None:
        self.calls.append(("clear",))

    def update(self, values, range_name, value_input_option) -> None:
        self.calls.append(("update", values, range_name, value_input_option))

    def freeze(self, rows) -> None:
        self.calls.append(("freeze", rows))


def test_the_sheet_is_cleared_before_writing_so_a_shorter_plan_leaves_no_stale_rows():
    worksheet = FakeWorksheet()
    rows = plan_rows(Plan(goal="x", weeks=(W42,)))
    write_rows(worksheet, rows)
    assert worksheet.calls == [("clear",), ("update", rows, "A1", "USER_ENTERED"),
                               ("freeze", 1)]


def test_a_missing_key_file_is_reported_by_name_without_touching_the_network(tmp_path):
    missing = tmp_path / "absent.json"
    with pytest.raises(SheetExportError, match="absent.json"):
        open_worksheet("sheet-id", str(missing))


def test_a_key_path_written_with_a_tilde_is_resolved_to_the_home_directory(
        tmp_path, monkeypatch):
    """.env is not a shell: `~` arrives verbatim and would be looked up as a folder
    literally named `~`."""
    from smart_coach import sheet_exporter

    opened = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sheet_exporter.gspread, "service_account",
                        lambda filename, scopes: opened.append(filename)
                        or pytest.fail("stop before the network"))
    with pytest.raises(pytest.fail.Exception):
        open_worksheet("sheet-id", "~/keys/sa.json")
    assert opened == [str(tmp_path / "keys" / "sa.json")]


# --- the command ---------------------------------------------------------------------

PLAN_TEXT = """# Plan

**Goal:** Berlin Half

## 2026-W42

- 2026-10-13 | Easy | 7.0 km | easy
"""


def test_export_reads_only_the_plan_and_never_logs_in_to_garmin(
        tmp_path, monkeypatch, capsys):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(PLAN_TEXT)
    sent = []
    monkeypatch.setattr(cli, "DEFAULT_PLAN", str(plan_file))
    monkeypatch.setattr(cli, "build_client", lambda *a, **k: pytest.fail("Garmin login"))
    monkeypatch.setattr(cli, "export_plan",
                        lambda plan, sheet_id, key_file: sent.append((sheet_id, key_file))
                        or len(plan.weeks[0].workouts))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/keys/sa.json")

    assert cli.main(["export"]) == 0
    assert sent == [("sheet-id", "/keys/sa.json")]
    assert "1" in capsys.readouterr().out


def test_export_without_its_settings_stops_with_the_name_of_what_is_missing(
        tmp_path, monkeypatch, capsys):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(PLAN_TEXT)
    monkeypatch.setattr(cli, "DEFAULT_PLAN", str(plan_file))

    assert cli.main(["export"]) == 1
    assert "GOOGLE_SHEET_ID" in capsys.readouterr().err


def test_a_failed_export_is_reported_as_a_stop_not_a_traceback(
        tmp_path, monkeypatch, capsys):
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(PLAN_TEXT)
    monkeypatch.setattr(cli, "DEFAULT_PLAN", str(plan_file))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "absent.json"))

    assert cli.main(["export"]) == 1
    assert "absent.json" in capsys.readouterr().err
