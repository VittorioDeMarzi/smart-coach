from datetime import date

from smart_coach.config import load_config
from smart_coach.guardrails import validate
from smart_coach.models import Regime, Snapshot, Step, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")
EMPTY_SNAPSHOT = Snapshot(as_of=date(2026, 9, 27), activities=(), chronic_load=None,
                          acute_load=None, vo2max=None, resting_hr=None, hrv_status=None)


def _workout(day: date, km: float, quality: bool = False) -> Workout:
    return Workout(
        day=day, name="Quality" if quality else "Easy", sport="running",
        steps=(Step(text=f"- {km:g}km Z2 Pace"),),
        total_km=km, is_quality=quality,
    )


def _week(workouts: tuple[Workout, ...], week_number: int = 40) -> Week:
    return Week(iso_year=2026, iso_week=week_number, workouts=workouts)


def _rules(violations) -> set[str]:
    return {v.rule for v in violations}


def test_two_quality_sessions_on_consecutive_days_is_a_violation():
    week = _week((
        _workout(date(2026, 9, 29), 10.0, quality=True),
        _workout(date(2026, 9, 30), 10.0, quality=True),
    ))
    assert "no_back_to_back_quality" in _rules(
        validate(week, None, EMPTY_SNAPSHOT, Regime.COLD, CONFIG))


def test_quality_sessions_with_an_easy_day_between_is_allowed():
    week = _week((
        _workout(date(2026, 9, 29), 10.0, quality=True),
        _workout(date(2026, 9, 30), 8.0),
        _workout(date(2026, 10, 1), 10.0, quality=True),
    ))
    assert "no_back_to_back_quality" not in _rules(
        validate(week, None, EMPTY_SNAPSHOT, Regime.COLD, CONFIG))


def test_missing_down_week_after_the_configured_span_is_a_violation():
    # Week 4 of a block with no reduction relative to week 3 must be flagged.
    previous = _week((_workout(date(2026, 9, 22), 40.0),), week_number=39)
    current = _week((_workout(date(2026, 9, 29), 42.0),), week_number=40)
    violations = validate(current, previous, EMPTY_SNAPSHOT, Regime.COLD, CONFIG,
                          weeks_since_down_week=CONFIG.guardrails.down_week_every)
    assert "down_week_required" in _rules(violations)


def test_a_reduced_week_satisfies_the_down_week_rule():
    previous = _week((_workout(date(2026, 9, 22), 40.0),), week_number=39)
    current = _week((_workout(date(2026, 9, 29), 28.0),), week_number=40)
    violations = validate(current, previous, EMPTY_SNAPSHOT, Regime.COLD, CONFIG,
                          weeks_since_down_week=CONFIG.guardrails.down_week_every)
    assert "down_week_required" not in _rules(violations)


def test_a_plain_easy_week_has_no_violations_from_these_rules():
    week = _week((
        _workout(date(2026, 9, 28), 8.0),
        _workout(date(2026, 9, 30), 8.0),
        _workout(date(2026, 10, 2), 8.0),
    ))
    violations = validate(week, None, EMPTY_SNAPSHOT, Regime.COLD, CONFIG)
    assert _rules(violations) & {
        "no_back_to_back_quality", "min_easy_days_after_quality", "down_week_required"
    } == set()
