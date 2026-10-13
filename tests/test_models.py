from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from smart_coach.models import Activity, Snapshot, Step, Week, Workout, next_iso_week


def _run(day: date, km: float) -> Activity:
    return Activity(day=day, sport="running", distance_km=km, duration_s=1800, avg_hr=140)


def test_models_are_immutable():
    activity = _run(date(2026, 9, 1), 10.0)
    with pytest.raises(FrozenInstanceError):
        activity.distance_km = 99.0


def test_running_km_excludes_other_sports():
    snapshot = Snapshot(
        as_of=date(2026, 9, 26),
        activities=(
            _run(date(2026, 9, 20), 10.0),
            Activity(day=date(2026, 9, 21), sport="cycling", distance_km=80.0,
                     duration_s=10800, avg_hr=130),
            Activity(day=date(2026, 9, 22), sport="walking", distance_km=5.0,
                     duration_s=3600, avg_hr=95),
        ),
        chronic_load=420.0, acute_load=450.0, vo2max=51.0,
        resting_hr=48, hrv_status="balanced",
    )
    assert snapshot.running_km == 10.0


def test_days_of_data_spans_first_to_last_activity():
    snapshot = Snapshot(
        as_of=date(2026, 9, 26),
        activities=(_run(date(2026, 9, 1), 8.0), _run(date(2026, 9, 26), 8.0)),
        chronic_load=None, acute_load=None, vo2max=None,
        resting_hr=None, hrv_status=None,
    )
    assert snapshot.days_of_data == 25


def test_days_of_data_is_zero_without_activities():
    snapshot = Snapshot(
        as_of=date(2026, 9, 26), activities=(),
        chronic_load=None, acute_load=None, vo2max=None,
        resting_hr=None, hrv_status=None,
    )
    assert snapshot.days_of_data == 0


def test_week_total_km_sums_workouts():
    week = Week(iso_year=2026, iso_week=40, workouts=(
        Workout(day=date(2026, 9, 28), name="Easy", sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=8.0, is_quality=False),
        Workout(day=date(2026, 9, 30), name="Fartlek", sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=10.0, is_quality=True),
    ))
    assert week.total_km == 18.0
    assert week.quality_days == (date(2026, 9, 30),)


def test_next_iso_week_reaches_week_53():
    """2026 has 53 ISO weeks: 1 January 2026 is a Thursday. Most years do not."""
    assert next_iso_week(date(2026, 12, 21)) == (2026, 53)


def test_next_iso_week_crosses_the_year_boundary():
    """The trap: incrementing the week number here yields week 54, which does not exist."""
    assert next_iso_week(date(2026, 12, 28)) == (2027, 1)


def test_next_iso_week_inside_a_year():
    assert next_iso_week(date(2026, 9, 26)) == (2026, 40)


def test_running_km_counts_only_the_load_window():
    """Activities are fetched over a long history to detect how long the athlete has been
    recording; volume must still be measured over the load window only."""
    snapshot = Snapshot(
        as_of=date(2026, 9, 26),
        activities=(_run(date(2026, 6, 10), 20.0),   # months ago: history, not volume
                    _run(date(2026, 9, 20), 10.0)),
        chronic_load=None, acute_load=None, vo2max=None,
        resting_hr=None, hrv_status=None, window_days=28,
    )
    assert snapshot.running_km == 10.0
    assert snapshot.weekly_running_km == 2.5


def test_days_of_data_spans_the_whole_history_not_just_the_window():
    """Reading 28 days and then measuring history inside them can never exceed 28: the
    athlete would read WARM forever however long they have been recording."""
    snapshot = Snapshot(
        as_of=date(2026, 9, 26),
        activities=(_run(date(2026, 6, 4), 8.0), _run(date(2026, 9, 20), 8.0)),
        chronic_load=None, acute_load=None, vo2max=None,
        resting_hr=None, hrv_status=None, window_days=28,
    )
    assert snapshot.days_of_data == 114
