"""The long run is judged against the longest run of the last 30 days.

A 2025 cohort of 5,205 Garmin users (Br J Sports Med) found injuries follow a single
session that exceeds the longest run of the previous 30 days by more than 10%, and found
no clear link with the week-to-week change or the acute/chronic ratio. The share of
weekly volume is a rule of thumb sized for 4-6 runs a week: at three runs it flagged
every sensible week, and under three it did not look at the long run at all.
"""

from datetime import date, timedelta

from smart_coach.config import load_config
from smart_coach.guardrails import recent_longest_run_km, validate, validate_structure
from smart_coach.models import Activity, Regime, Severity, Snapshot, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")
SUNDAY_W39 = date(2026, 9, 27)
MONDAY_W40 = date(2026, 9, 28)


def _week(iso_week: int, *kms: float) -> Week:
    """Runs on Tuesday, Thursday, Sunday, in that order."""
    monday = date.fromisocalendar(2026, iso_week, 1)
    return Week(iso_year=2026, iso_week=iso_week, workouts=tuple(
        Workout(day=monday + timedelta(days=offset), name=f"Run {offset}",
                sport="running", steps=(), total_km=km, is_quality=False)
        for offset, km in zip((1, 3, 6), kms)))


def _ran(day: date, km: float, sport: str = "running") -> Activity:
    return Activity(day=day, sport=sport, distance_km=km, duration_s=1800, avg_hr=140)


def _snapshot(*activities: Activity, as_of: date = SUNDAY_W39) -> Snapshot:
    return Snapshot(as_of=as_of, activities=activities, chronic_load=400.0,
                    acute_load=420.0, vo2max=None, resting_hr=None, hrv_status=None)


def _severity(week, snapshot, rule="long_run_spike", planned=()) -> Severity | None:
    found = {v.rule: v.severity for v in validate(
        week, None, snapshot, Regime.HOT, CONFIG, planned_weeks=planned)}
    return found.get(rule)


LONGEST_IS_15_7 = _snapshot(_ran(date(2026, 9, 13), 15.7), _ran(date(2026, 9, 22), 9.0),
                            _ran(date(2026, 9, 20), 60.0, sport="cycling"))


def test_a_long_run_within_ten_percent_of_the_recent_longest_is_clean():
    assert _severity(_week(40, 7.0, 7.0, 17.0), LONGEST_IS_15_7) is None


def test_a_long_run_more_than_ten_percent_longer_warns():
    assert _severity(_week(40, 7.0, 7.0, 18.0), LONGEST_IS_15_7) is Severity.WARN


def test_a_long_run_more_than_thirty_percent_longer_blocks():
    assert _severity(_week(40, 7.0, 7.0, 21.0), LONGEST_IS_15_7) is Severity.BLOCK


def test_a_week_of_one_or_two_runs_is_checked_too():
    """The share rule never looked at these weeks."""
    assert _severity(_week(40, 21.0), LONGEST_IS_15_7) is Severity.BLOCK
    assert _severity(_week(40, 14.0, 21.0), LONGEST_IS_15_7) is Severity.BLOCK


def test_the_reference_window_is_the_thirty_days_before_the_week():
    inside = _snapshot(_ran(MONDAY_W40 - timedelta(days=30), 20.0))
    outside = _snapshot(_ran(MONDAY_W40 - timedelta(days=31), 20.0),
                        _ran(date(2026, 9, 22), 10.0))
    assert recent_longest_run_km(_week(40, 8.0), (), inside, CONFIG) == 20.0
    assert recent_longest_run_km(_week(40, 8.0), (), outside, CONFIG) == 10.0


def test_with_no_run_in_the_window_the_rule_declines():
    """Nothing to compare with. For the week being published the load is then
    unusable too, and the absolute long-run cap governs."""
    assert _severity(_week(40, 8.0, 8.0, 12.0), _snapshot()) is None


def test_a_long_run_still_ahead_counts_as_planned():
    """Checking W43 on the Sunday of W39: the 16 km of W42 exists only in the plan."""
    planned = (_week(41, 7.0, 7.0, 14.5), _week(42, 7.0, 7.0, 16.0))
    assert recent_longest_run_km(_week(43, 8.0), planned, _snapshot(), CONFIG) == 16.0
    assert _severity(_week(43, 7.0, 7.0, 17.5), _snapshot(), planned=planned) is None


def test_a_planned_run_whose_day_has_passed_does_not_count():
    """A 20 km long run written in the plan and never run licenses nothing."""
    skipped = Week(iso_year=2026, iso_week=39, workouts=(
        Workout(day=date(2026, 9, 22), name="Long run", sport="running", steps=(),
                total_km=20.0, is_quality=False),))
    snapshot = _snapshot(_ran(date(2026, 9, 24), 10.0))
    assert recent_longest_run_km(_week(40, 8.0), (skipped,), snapshot, CONFIG) == 10.0


def test_the_rule_is_part_of_the_outlook_over_the_whole_cycle():
    rules = {v.rule for v in validate_structure(
        _week(40, 7.0, 7.0, 21.0), None, LONGEST_IS_15_7, CONFIG)}
    assert "long_run_spike" in rules


# --- what is left of the share rule --------------------------------------------------

def test_three_runs_with_a_proper_long_run_are_no_longer_refused_for_its_share():
    """7 + 7 + 17 km: the long run is 55% of the week, the shape of any three-run plan
    for a half marathon."""
    assert _severity(_week(40, 7.0, 7.0, 17.0), LONGEST_IS_15_7, "long_run_share") is None


def test_a_week_that_is_almost_only_its_long_run_is_flagged():
    assert _severity(_week(40, 4.0, 4.0, 15.0), LONGEST_IS_15_7,
                     "long_run_share") is Severity.WARN


def test_the_share_is_not_computed_on_two_runs():
    """8 + 14 km is 64% and perfectly ordinary; the spike rule guards those weeks."""
    assert _severity(_week(40, 8.0, 14.0), LONGEST_IS_15_7, "long_run_share") is None
