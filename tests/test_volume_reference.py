"""The volume ramp is measured against what the athlete has recently sustained.

Comparing a week only with the one before it contradicts the down-week rule: after a
lighter week the ladder restarts from the bottom, and a fortnight abroad locks the
athlete out of a level they held a month earlier. The reference is therefore the larger
of last week and the four-week average; returning to a recently run level is a warning,
exceeding everything recent is a block.
"""

from datetime import date, timedelta

from smart_coach.config import load_config
from smart_coach.guardrails import recent_week_km, validate
from smart_coach.models import Activity, Regime, Severity, Snapshot, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")
RULE = "weekly_volume_increase"
SUNDAY_W39 = date(2026, 9, 27)


def _monday(iso_week: int) -> date:
    return date.fromisocalendar(2026, iso_week, 1)


def _week(iso_week: int, *kms: float) -> Week:
    """Runs on Monday, Wednesday, Friday, Sunday, in that order."""
    return Week(iso_year=2026, iso_week=iso_week, workouts=tuple(
        Workout(day=_monday(iso_week) + timedelta(days=2 * n), name=f"Run {n}",
                sport="running", steps=(), total_km=km, is_quality=False)
        for n, km in enumerate(kms)))


def _ran(iso_week: int, km: float, weekday: int = 2) -> Activity:
    return Activity(day=_monday(iso_week) + timedelta(days=weekday), sport="running",
                    distance_km=km, duration_s=1800, avg_hr=140)


def _snapshot(*activities: Activity, as_of: date = SUNDAY_W39) -> Snapshot:
    return Snapshot(as_of=as_of, activities=activities, chronic_load=400.0,
                    acute_load=420.0, vo2max=None, resting_hr=None, hrv_status=None)


def _severity(week, previous, snapshot, planned=()) -> Severity | None:
    found = {v.rule: v.severity for v in validate(
        week, previous, snapshot, Regime.HOT, CONFIG, planned_weeks=planned)}
    return found.get(RULE)


# --- after a down week ---------------------------------------------------------------

BUILD_THEN_DOWN = (_ran(36, 20.0), _ran(37, 22.0), _ran(38, 24.0), _ran(39, 19.0))


def test_the_week_after_a_down_week_may_return_to_the_recent_level():
    """Average 21.25 km: 23 km is within +10% of it, although it is +21% on the down
    week alone."""
    assert _severity(_week(40, 8.0, 7.0, 8.0), _week(39, 19.0),
                     _snapshot(*BUILD_THEN_DOWN)) is None


def test_returning_exactly_to_the_recent_peak_warns_but_never_blocks():
    assert _severity(_week(40, 8.0, 8.0, 8.0), _week(39, 19.0),
                     _snapshot(*BUILD_THEN_DOWN)) is Severity.WARN


def test_exceeding_both_the_ramp_and_the_recent_peak_blocks():
    assert _severity(_week(40, 9.0, 9.0, 9.0), _week(39, 19.0),
                     _snapshot(*BUILD_THEN_DOWN)) is Severity.BLOCK


# --- after a fortnight of almost nothing ---------------------------------------------

AFTER_A_TRIP = (_ran(36, 28.0), _ran(37, 30.0), _ran(38, 5.0), _ran(39, 5.0))


def test_after_two_light_weeks_the_clean_limit_follows_the_average():
    assert _severity(_week(40, 9.0, 9.0), _week(39, 5.0),
                     _snapshot(*AFTER_A_TRIP)) is None  # 18 km against 17 km average


def test_after_two_light_weeks_the_level_held_before_them_is_a_warning_not_a_block():
    """Under the old rule anything above 6.25 km was refused."""
    assert _severity(_week(40, 10.0, 10.0, 10.0), _week(39, 5.0),
                     _snapshot(*AFTER_A_TRIP)) is Severity.WARN


def test_after_two_light_weeks_more_than_the_level_held_before_them_blocks():
    assert _severity(_week(40, 11.0, 10.0, 10.0), _week(39, 5.0),
                     _snapshot(*AFTER_A_TRIP)) is Severity.BLOCK


# --- the first week of a plan --------------------------------------------------------

UNEVEN_MONTH = (_ran(36, 8.0), _ran(37, 32.0), _ran(38, 14.0), _ran(39, 16.0))


def test_the_first_week_follows_the_same_rule_as_every_other_week():
    """The outcome must not depend on whether plan.md happens to hold a previous week:
    17.5 km/week on average, 32 km at the peak."""
    snapshot = _snapshot(*UNEVEN_MONTH)
    assert _severity(_week(40, 6.0, 6.0, 7.0), None, snapshot) is None
    assert _severity(_week(40, 8.0, 8.0, 9.0), None, snapshot) is Severity.WARN
    assert _severity(_week(40, 11.0, 11.0, 11.0), None, snapshot) is Severity.BLOCK


def test_with_nothing_run_and_nothing_planned_the_rule_declines():
    """There is no reference to measure from; the absolute cap governs that case."""
    assert _severity(_week(40, 8.0), None, _snapshot()) is None


# --- weeks that have not been run yet ------------------------------------------------

def test_a_week_still_ahead_counts_for_what_is_planned():
    """Validating W43 on the Sunday of W39: W40-W42 exist only in the plan."""
    planned = (_week(40, 20.0), _week(41, 22.0), _week(42, 19.0))
    assert recent_week_km(_week(43, 23.0), planned, _snapshot(_ran(39, 24.0)),
                          CONFIG) == (19.0, 22.0, 20.0, 24.0)
    assert _severity(_week(43, 23.0), planned[-1], _snapshot(_ran(39, 24.0)),
                     planned) is None


def test_a_week_still_ahead_and_absent_from_the_plan_counts_as_zero():
    """Fail closed: an unknown week must not raise the reference."""
    assert recent_week_km(_week(43, 23.0), (), _snapshot(_ran(39, 24.0)),
                          CONFIG) == (0.0, 0.0, 0.0, 24.0)


def test_a_week_in_progress_counts_what_was_run_plus_what_is_still_planned():
    """Wednesday of W40: Monday's run happened (6 km against 8 planned), Wednesday's
    plan entry is ignored because the day is over, Sunday's 10 km is still ahead."""
    wednesday = _monday(40) + timedelta(days=2)
    in_progress = Week(iso_year=2026, iso_week=40, workouts=tuple(
        Workout(day=_monday(40) + timedelta(days=offset), name="Run", sport="running",
                steps=(), total_km=km, is_quality=False)
        for offset, km in ((0, 8.0), (2, 8.0), (6, 10.0))))
    snapshot = _snapshot(_ran(40, 6.0, weekday=0), as_of=wednesday)
    assert recent_week_km(_week(41, 20.0), (in_progress,), snapshot, CONFIG)[0] == 16.0
