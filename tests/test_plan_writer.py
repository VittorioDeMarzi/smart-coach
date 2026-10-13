from datetime import date

import pytest

from smart_coach.models import Plan, Step, Week, Workout
from smart_coach.plan_writer import (
    PlanParseError,
    read_plan,
    weeks_since_down_week,
    write_plan,
)

PLAN_TEXT = """# Plan

**Goal:** Half marathon, Florence, 2026-11-29

## 2026-W40

- 2026-09-28 | Easy | 8.0 km | easy
- 2026-09-30 | Fartlek | 10.0 km | quality
- 2026-10-04 | Long run | 16.0 km | easy

## 2026-W41

- 2026-10-05 | Easy | 8.0 km | easy
"""


def test_reads_weeks_and_workouts(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text(PLAN_TEXT)
    plan = read_plan(path)

    assert plan.goal == "Half marathon, Florence, 2026-11-29"
    assert len(plan.weeks) == 2
    assert plan.weeks[0].iso_year == 2026
    assert plan.weeks[0].iso_week == 40
    assert plan.weeks[0].total_km == 34.0
    assert plan.weeks[0].quality_days == (date(2026, 9, 30),)


def test_round_trip_preserves_the_plan(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text(PLAN_TEXT)
    original = read_plan(path)

    out = tmp_path / "again.md"
    write_plan(original, out)
    assert read_plan(out) == original


def test_malformed_workout_line_reports_the_line_number(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("""# Plan

**Goal:** Test

## 2026-W40

- 2026-09-28 | Easy | eight kilometres | easy
""")
    with pytest.raises(PlanParseError) as caught:
        read_plan(path)
    assert caught.value.line_number == 7
    assert "eight kilometres" in str(caught.value)


def test_workout_outside_any_week_heading_is_rejected(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("""# Plan

**Goal:** Test

- 2026-09-28 | Easy | 8.0 km | easy
""")
    with pytest.raises(PlanParseError) as caught:
        read_plan(path)
    assert caught.value.line_number == 5


def _weeks(volumes: list[float]) -> tuple[Week, ...]:
    return tuple(
        Week(iso_year=2026, iso_week=40 + n, workouts=(
            Workout(day=date(2026, 9, 28), name="Easy", sport="running",
                    steps=(Step(text="- 5km Z2 Pace"),),
                    total_km=km, is_quality=False),))
        for n, km in enumerate(volumes)
    )


def test_weeks_since_down_week_counts_back_to_the_last_reduction():
    # Volumes 30, 33, 36, 26, 29, 32: the drop to 26 is at index 3.
    weeks = _weeks([30.0, 33.0, 36.0, 26.0, 29.0, 32.0])
    assert weeks_since_down_week(weeks, 5) == 2


def test_weeks_since_down_week_without_any_reduction_returns_the_index():
    """Four weeks of pure build: the down-week rule must fire, not stay silent."""
    weeks = _weeks([30.0, 33.0, 36.0, 39.0])
    assert weeks_since_down_week(weeks, 3) == 3


def test_missing_goal_is_rejected(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n## 2026-W40\n\n- 2026-09-28 | Easy | 8.0 km | easy\n")
    with pytest.raises(PlanParseError):
        read_plan(path)
