from datetime import date

import pytest

from smart_coach.config import load_config
from smart_coach.guardrails import validate
from smart_coach.models import Regime, Severity, Snapshot, Step, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")
EMPTY_SNAPSHOT = Snapshot(as_of=date(2026, 9, 27), activities=(), chronic_load=None,
                          acute_load=None, resting_hr=None, hrv_status=None)


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


def _opening_with(*texts: str) -> Week:
    return _week((Workout(day=date(2026, 9, 29), name="Easy", sport="running",
                          steps=tuple(Step(text=t) for t in texts),
                          total_km=8.0, is_quality=False),))


@pytest.mark.parametrize("first", [
    "- Warmup 2km 5:50/km-6:30/km Pace intensity=warmup",
    "- Warmup 1km 5:50/km-6:30/km Pace intensity=warmup",
])
def test_a_warmup_that_ends_at_its_distance_is_accepted(first):
    week = _opening_with(first, "- 6km 5:40/km-6:05/km Pace")
    assert "warmup_required" not in _rules(
        validate(week, None, EMPTY_SNAPSHOT, Regime.COLD, CONFIG))


@pytest.mark.parametrize("steps", [
    # No warm-up: the athlete starts straight on the main set.
    ("- 5.5km 5:40/km-6:05/km Pace",),
    # On the lap button: a press meant to end it skips the step after it.
    ("- Warmup press lap 2km 5:50/km-6:30/km Pace intensity=warmup",),
    # Not one of the allowed distances.
    ("- Warmup 3km 5:50/km-6:30/km Pace intensity=warmup",),
    ("- Warmup 1.5km 5:50/km-6:30/km Pace intensity=warmup",),
    # Timed, not ending at a distance.
    ("- Warmup 10m 5:50/km-6:30/km Pace intensity=warmup",),
    # A cue alone does not make it a warm-up step on the watch.
    ("- Warmup 2km 5:50/km-6:30/km Pace",),
    # No steps: the fallback publishes a single step with no warm-up.
    (),
])
def test_a_workout_without_a_proper_warmup_is_refused(steps):
    week = _opening_with(*steps)
    found = [v for v in validate(week, None, EMPTY_SNAPSHOT, Regime.COLD, CONFIG)
             if v.rule == "warmup_required"]
    assert len(found) == 1 and found[0].severity is Severity.BLOCK


def test_a_day_already_past_is_not_held_to_the_warmup_rule():
    """The push never rewrites a day before today, so an old session must not stop the
    rest of the week from being republished."""
    week = _opening_with("- 5.5km 5:40/km-6:05/km Pace")
    later = Snapshot(as_of=date(2026, 9, 30), activities=(), chronic_load=None,
                     acute_load=None, resting_hr=None, hrv_status=None)
    assert "warmup_required" not in _rules(
        validate(week, None, later, Regime.COLD, CONFIG))
