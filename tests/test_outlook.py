"""Checking the whole cycle, not only the week about to be published.

The plan holds every week up to the race so the athlete can see where it is going. The
weeks ahead are checked for shape only: the athlete's current load says nothing about a
week four months away, and applying it there would refuse the whole future.
"""

from datetime import date, timedelta

from smart_coach import cli
from smart_coach.models import Activity, Plan, Snapshot, Week, Workout

CONFIG_PATH = "tests/fixtures/config.toml"
AS_OF = date(2026, 9, 27)  # Sunday of W39


def _week(iso_week: int, *kms: float, iso_year: int = 2026) -> Week:
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    return Week(iso_year=iso_year, iso_week=iso_week, workouts=tuple(
        Workout(day=monday + timedelta(days=2 * n), name=f"Run {n}", sport="running",
                steps=(), total_km=km, is_quality=False)
        for n, km in enumerate(kms)))


def _snapshot(*weekly_km: float, load: float | None = 400.0) -> Snapshot:
    """`weekly_km` are the weeks ending at W39, oldest first."""
    first = 39 - len(weekly_km) + 1
    runs = tuple(Activity(day=date.fromisocalendar(2026, first + n, 3), sport="running",
                          distance_km=km, duration_s=1800, avg_hr=140)
                 for n, km in enumerate(weekly_km))
    return Snapshot(as_of=AS_OF, activities=runs, chronic_load=load, acute_load=load,
                    vo2max=None, resting_hr=None, hrv_status=None)


def test_the_previous_week_is_the_adjacent_iso_week_or_nothing():
    """After a fortnight with no running the plan skips two headings. The week before
    the gap is not 'last week', and measuring the ramp from it would wave the return
    through."""
    plan = Plan(goal="x", weeks=(_week(50, 10.0), _week(51, 10.0),
                                 _week(1, 10.0, iso_year=2027)))
    assert cli.previous_week_of(plan, 0) is None
    assert cli.previous_week_of(plan, 1) is plan.weeks[0]
    assert cli.previous_week_of(plan, 2) is None


def test_the_previous_week_is_found_across_a_53_week_year():
    plan = Plan(goal="x", weeks=(_week(53, 10.0), _week(1, 10.0, iso_year=2027)))
    assert cli.previous_week_of(plan, 1) is plan.weeks[0]


def test_a_steady_build_with_a_down_week_passes_the_outlook(capsys):
    plan = Plan(goal="x", weeks=(_week(40, 10.0, 10.0), _week(41, 11.0, 11.0),
                                 _week(42, 12.0, 12.0), _week(43, 9.0, 10.0),
                                 _week(44, 12.0, 12.0)))
    assert cli.outlook_command(plan, 1, _snapshot(18.0, 19.0, 19.0, 19.0),
                               CONFIG_PATH) == 0
    assert "BLOCK" not in capsys.readouterr().out


def test_a_jump_in_a_week_far_ahead_fails_the_outlook(capsys):
    plan = Plan(goal="x", weeks=(_week(40, 10.0, 10.0), _week(41, 11.0, 11.0),
                                 _week(42, 20.0, 20.0)))
    assert cli.outlook_command(plan, 1, _snapshot(18.0, 19.0, 19.0, 19.0),
                               CONFIG_PATH) == 1
    out = capsys.readouterr().out
    assert "2026-W42" in out and "weekly_volume_increase" in out


def test_the_outlook_does_not_apply_todays_load_to_future_weeks(capsys):
    """With the load missing, a 60 km week is refused for publication today. Months
    ahead the same absence means nothing."""
    plan = Plan(goal="x", weeks=(_week(40, 20.0, 20.0, 20.0), _week(41, 20.0, 21.0, 21.0)))
    assert cli.outlook_command(plan, 1, _snapshot(60.0, 60.0, 60.0, 60.0, load=None),
                               CONFIG_PATH) == 0
    assert "absolute_volume_cap" not in capsys.readouterr().out
