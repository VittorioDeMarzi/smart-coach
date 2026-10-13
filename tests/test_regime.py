from datetime import date, timedelta

from smart_coach.config import load_config
from smart_coach.models import Activity, Regime, Snapshot
from smart_coach.regime import (
    determine_regime,
    effective_history_days,
    longest_gap_days,
)

CONFIG = load_config("tests/fixtures/config.toml")


def _snapshot(days: list[int], as_of: date = date(2026, 9, 30)) -> Snapshot:
    """Snapshot with one run on each of `days` days before `as_of`."""
    return Snapshot(
        as_of=as_of,
        activities=tuple(
            Activity(day=as_of - timedelta(days=d), sport="running",
                     distance_km=8.0, duration_s=1800, avg_hr=140)
            for d in sorted(days, reverse=True)
        ),
        chronic_load=400.0, acute_load=420.0, vo2max=50.0,
        resting_hr=50, hrv_status="balanced",
    )


def test_no_activities_is_cold():
    empty = Snapshot(as_of=date(2026, 9, 30), activities=(), chronic_load=None,
                     acute_load=None, vo2max=None, resting_hr=None, hrv_status=None)
    assert determine_regime(empty, CONFIG) is Regime.COLD


def test_thirteen_days_is_cold():
    assert determine_regime(_snapshot([13, 10, 7, 4, 0]), CONFIG) is Regime.COLD


def test_fourteen_days_is_warm():
    assert determine_regime(_snapshot([14, 10, 7, 4, 0]), CONFIG) is Regime.WARM


def test_twentyseven_days_is_warm():
    assert determine_regime(_snapshot([27, 20, 14, 7, 0]), CONFIG) is Regime.WARM


def test_twentyeight_days_is_hot():
    assert determine_regime(_snapshot([28, 21, 14, 7, 0]), CONFIG) is Regime.HOT


def test_long_gap_forces_cold_despite_long_history():
    # A year of history, then a long silence, then two runs last week.
    # Without the decay rule this reads as HOT and plans from a stale baseline.
    snapshot = _snapshot([365, 300, 200, 100, 75, 5, 2])
    assert longest_gap_days(snapshot) > CONFIG.regime.decay_threshold_weeks * 7
    assert determine_regime(snapshot, CONFIG) is Regime.COLD


def test_config_values_load_from_toml():
    assert CONFIG.regime.decay_threshold_weeks == 3
    assert CONFIG.guardrails.acwr_max == 1.3
    assert CONFIG.baseline.floor_weekly_km == 0.0


# --- history is counted from the athlete's return, not from the first run ever -------

def test_history_restarts_after_a_long_interruption():
    """Three months off then a month back: the effective history is the month back.

    Measuring the longest gap ever means a single old layoff pins the athlete to COLD
    for good, however consistent they become afterwards.
    """
    # Runs every 3 days for the last 26 days, and an old block before an 80-day gap.
    recent = list(range(0, 25, 3))   # a run every 3 days, the oldest 24 days back
    old = [106, 112, 118, 124]
    snapshot = _snapshot(recent + old)
    assert effective_history_days(snapshot, CONFIG) == 24
    assert determine_regime(snapshot, CONFIG) is Regime.WARM


def test_the_same_athlete_reaches_hot_once_the_return_is_long_enough():
    recent = list(range(0, 31, 3))
    old = [110, 116, 122]
    snapshot = _snapshot(recent + old)
    assert effective_history_days(snapshot, CONFIG) == 30
    assert determine_regime(snapshot, CONFIG) is Regime.HOT


def test_history_without_any_interruption_runs_from_the_first_activity():
    snapshot = _snapshot(list(range(0, 61, 4)))
    assert effective_history_days(snapshot, CONFIG) == 60


def test_an_athlete_who_has_currently_stopped_has_no_effective_history():
    """Stopped 40 days ago and not back: a layoff in progress, not a long history."""
    snapshot = _snapshot([40, 45, 50, 55, 60])
    assert effective_history_days(snapshot, CONFIG) == 0
    assert determine_regime(snapshot, CONFIG) is Regime.COLD


# --- a hole in the DATA is not a hole in the TRAINING (spec 8.5) ---------------------

def _config_with_gap(start: date, end: date) -> object:
    from dataclasses import replace as dc_replace

    from smart_coach.config import DataGap, HistoryConfig
    return dc_replace(CONFIG, history=HistoryConfig(
        data_gaps=(DataGap(start=start, end=end, reason="watch being replaced"),)))


def test_a_declared_data_gap_does_not_reset_the_history():
    """Watch broken, athlete still running. Garmin cannot tell this from a layoff, and
    treating it as one starts the plan far below what the athlete can handle."""
    as_of = date(2026, 9, 30)
    before = [176, 170, 160, 150, 140, 130, 120, 110]   # up to 12 June
    after = list(range(0, 31, 3))                        # back from 30 August
    snapshot = _snapshot(before + after, as_of=as_of)

    assert effective_history_days(snapshot, CONFIG) == 30           # undeclared: a layoff
    declared = _config_with_gap(as_of - timedelta(days=110), as_of - timedelta(days=30))
    assert effective_history_days(snapshot, declared) == 176        # declared: continuous
    assert determine_regime(snapshot, declared) is Regime.HOT


def test_a_partially_declared_gap_is_still_excused():
    """The athlete remembers roughly when the watch died, not to the day."""
    as_of = date(2026, 9, 30)
    snapshot = _snapshot([176, 170, 160, 150, 140, 130, 120, 110] + list(range(0, 31, 3)),
                         as_of=as_of)
    # Declared a week narrower on each side than the real hole.
    declared = _config_with_gap(as_of - timedelta(days=103), as_of - timedelta(days=37))
    assert effective_history_days(snapshot, declared) == 176


def test_a_declaration_that_covers_almost_nothing_does_not_excuse_the_gap():
    as_of = date(2026, 9, 30)
    snapshot = _snapshot([176, 170, 160, 150, 140, 130, 120, 110] + list(range(0, 31, 3)),
                         as_of=as_of)
    declared = _config_with_gap(as_of - timedelta(days=60), as_of - timedelta(days=55))
    assert effective_history_days(snapshot, declared) == 30


def test_a_declared_gap_does_not_excuse_a_layoff_still_in_progress():
    """Declaring old holes must not make an athlete who stopped last month look active."""
    as_of = date(2026, 9, 30)
    snapshot = _snapshot([40, 45, 50, 55], as_of=as_of)
    declared = _config_with_gap(as_of - timedelta(days=60), as_of - timedelta(days=50))
    assert effective_history_days(snapshot, declared) == 0
