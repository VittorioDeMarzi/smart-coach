from datetime import date

from smart_coach.config import load_config
from smart_coach.guardrails import validate
from smart_coach.models import Regime, Snapshot, Step, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")


def _snapshot(chronic: float | None, acute: float | None = 400.0) -> Snapshot:
    return Snapshot(as_of=date(2026, 9, 27), activities=(), chronic_load=chronic,
                    acute_load=acute, vo2max=None, resting_hr=None, hrv_status=None)


def _week(total_km: float, longest_km: float | None = None) -> Week:
    """Long run plus three equal easy runs, so the long run really is the longest."""
    longest = longest_km if longest_km is not None else total_km * 0.3
    each = (total_km - longest) / 3
    days = (date(2026, 9, 27), date(2026, 9, 29), date(2026, 10, 1), date(2026, 10, 3))
    workouts = tuple(
        Workout(day=day, name=name, sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=km, is_quality=False)
        for day, name, km in zip(days, ("Long", "Easy", "Easy", "Easy"),
                                 (longest, each, each, each))
    )
    return Week(iso_year=2026, iso_week=40, workouts=workouts)


def _rules(violations) -> set[str]:
    return {v.rule for v in violations}


def test_volume_increase_above_the_cap_is_a_violation():
    previous = _week(40.0)
    current = _week(46.0)  # +15%, cap is 10%
    assert "weekly_volume_increase" in _rules(
        validate(current, previous, _snapshot(400.0), Regime.HOT, CONFIG))


def test_volume_increase_within_the_cap_is_allowed():
    previous = _week(40.0)
    current = _week(43.0)  # +7.5%
    assert "weekly_volume_increase" not in _rules(
        validate(current, previous, _snapshot(400.0), Regime.HOT, CONFIG))


def test_long_run_share_above_the_cap_is_a_violation():
    week = _week(40.0, longest_km=26.0)  # 65%, cap is 60%
    assert "long_run_share" in _rules(
        validate(week, None, _snapshot(400.0), Regime.HOT, CONFIG))


def test_acwr_above_the_band_is_a_violation():
    # acute 700 / chronic 400 = 1.75, band tops out at 1.3
    assert "acwr_band" in _rules(
        validate(_week(40.0), None, _snapshot(400.0, acute=700.0), Regime.HOT, CONFIG))


def test_acwr_inside_the_band_is_allowed():
    assert "acwr_band" not in _rules(
        validate(_week(40.0), None, _snapshot(400.0, acute=440.0), Regime.HOT, CONFIG))


def test_missing_chronic_load_falls_back_to_an_absolute_cap():
    """The point of the whole design: a missing metric must not disable the check.

    This week passes every nominal rule. With chronic load absent it must still be
    refused, because the fallback cap is stricter than what the nominal rules allow.
    """
    week = _week(60.0, longest_km=18.0)  # above fallback_weekly_km_cap = 45
    with_data = validate(week, _week(58.0), _snapshot(600.0, acute=620.0),
                         Regime.HOT, CONFIG)
    without_data = validate(week, _week(58.0), _snapshot(None, acute=None),
                            Regime.COLD, CONFIG)

    assert "absolute_volume_cap" not in _rules(with_data)
    assert "absolute_volume_cap" in _rules(without_data)
    assert len(without_data) > len(with_data), "fallback must be stricter, not absent"


def test_missing_chronic_load_never_skips_silently():
    week = _week(30.0, longest_km=10.0)  # inside every fallback cap
    violations = validate(week, None, _snapshot(None, acute=None), Regime.COLD, CONFIG)
    assert "acwr_band" not in _rules(violations)  # not computable, so not claimed
    assert _rules(violations) == set()  # and a genuinely safe week still passes
