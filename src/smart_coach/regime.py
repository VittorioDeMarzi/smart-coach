"""Data regime detection. The first decision of every run (spec 8.2)."""

from __future__ import annotations

from datetime import date, timedelta

from smart_coach.config import Config
from smart_coach.models import RUNNING_SPORT, Regime, Snapshot


def longest_gap_days(snapshot: Snapshot) -> int:
    """Longest interruption between consecutive runs, in days. Reporting only."""
    days = _running_days(snapshot)
    if len(days) < 2:
        return 0
    return max((b - a).days for a, b in zip(days, days[1:]))


def _running_days(snapshot: Snapshot) -> list[date]:
    return sorted({a.day for a in snapshot.activities if a.sport == RUNNING_SPORT})


def _declared_cover_days(start: date, end: date, config: Config) -> int:
    """Days of the interval [start, end] declared as data gaps rather than real breaks."""
    covered = 0
    for gap in config.history.data_gaps:
        overlap_start = max(start, gap.start)
        overlap_end = min(end, gap.end)
        if overlap_start < overlap_end:
            covered += (overlap_end - overlap_start).days
    return covered


def _is_real_interruption(start: date, end: date, config: Config) -> bool:
    """Whether a hole between two activities means the athlete actually stopped.

    A declared data gap is subtracted first; what remains uncovered is judged against
    the threshold, so an approximate declaration still works — the athlete remembers
    roughly when the watch died, not to the day.
    """
    uncovered = (end - start).days - _declared_cover_days(start, end, config)
    return uncovered > config.regime.decay_threshold_weeks * 7


def effective_history_days(snapshot: Snapshot, config: Config) -> int:
    """Days of history that still count, measured from the athlete's return.

    Spec 8.4 says a long interruption invalidates the baseline: a return is a beginning,
    not a continuation. So history runs from the first activity AFTER the last long
    interruption, not from the first activity ever — otherwise one old layoff pins the
    athlete to COLD permanently, however consistent they become afterwards.

    A layoff still in progress (nothing recorded for longer than the threshold) leaves
    no effective history at all.
    """
    days = _running_days(snapshot)
    if not days:
        return 0
    if _is_real_interruption(days[-1], snapshot.as_of, config):
        return 0
    start = days[0]
    for earlier, later in zip(days, days[1:]):
        if _is_real_interruption(earlier, later, config):
            start = later
    return (snapshot.as_of - start).days


def determine_regime(snapshot: Snapshot, config: Config) -> Regime:
    """Cold / Warm / Hot from the amount of history that still counts."""
    days = effective_history_days(snapshot, config)
    if days <= config.regime.cold_max_days:
        return Regime.COLD
    if days <= config.regime.warm_max_days:
        return Regime.WARM
    return Regime.HOT
