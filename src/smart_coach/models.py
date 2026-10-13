"""Immutable domain types. No I/O, no dependencies on other project modules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum

RUNNING_SPORT = "running"


class Regime(Enum):
    """How much trustworthy history the athlete has. Drives which guardrails apply."""

    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


@dataclass(frozen=True)
class Activity:
    day: date
    sport: str
    distance_km: float
    duration_s: int
    avg_hr: int | None
    # Where it was run and how much it climbed. Context for whoever reads pace and heart
    # rate: the same effort on a hilly route elsewhere produces different numbers.
    location: str | None = None
    elevation_gain_m: float | None = None


@dataclass(frozen=True)
class Snapshot:
    """What Garmin knows about the athlete at a point in time. `None` means missing,
    never zero: a missing metric and a zero metric lead to opposite decisions."""

    as_of: date
    activities: tuple[Activity, ...]
    chronic_load: float | None
    acute_load: float | None
    vo2max: float | None
    resting_hr: int | None
    hrv_status: str | None
    window_days: int = 28

    @property
    def running_km(self) -> float:
        """Running volume inside the load window.

        `activities` covers a longer history so `days_of_data` can tell how long the
        athlete has been recording; volume must not be summed over all of it.
        """
        return sum(a.distance_km for a in self.recent_runs)

    @property
    def recent_runs(self) -> tuple[Activity, ...]:
        """Running activities inside the load window, oldest first."""
        earliest = self.as_of - timedelta(days=self.window_days - 1)
        return tuple(sorted(
            (a for a in self.activities
             if a.sport == RUNNING_SPORT and earliest <= a.day <= self.as_of),
            key=lambda a: a.day))

    @property
    def days_of_data(self) -> int:
        """Span of RUNNING history, measured from `as_of`.

        Non-running activities do not count: a month of auto-detected walks is not a
        month of running history. Measuring from `as_of` rather than from the last
        activity means a rest day on the window boundary does not shrink the span.
        """
        days = [a.day for a in self.activities if a.sport == RUNNING_SPORT]
        if not days:
            return 0
        return (self.as_of - min(days)).days

    @property
    def weekly_running_km(self) -> float:
        """Average weekly running volume actually recorded over the snapshot window."""
        weeks = self.window_days / 7
        return self.running_km / weeks if weeks else 0.0

    def running_km_on(self, day: date) -> float:
        """Kilometres run on that day. Zero when nothing was recorded: the callers
        average over weeks, where an unrecorded day can only make them stricter."""
        return sum(a.distance_km for a in self.activities
                   if a.sport == RUNNING_SPORT and a.day == day)

    def longest_run_km_between(self, first: date, last: date) -> float:
        """Longest single run recorded from `first` to `last`, both included. Zero when
        there is none: the caller treats that as 'no reference', not as a short run."""
        return max((a.distance_km for a in self.activities
                    if a.sport == RUNNING_SPORT and first <= a.day <= last), default=0.0)

    def running_km_in_iso_week(self, iso_year: int, iso_week: int) -> float | None:
        """Kilometres actually run in that ISO week, or None when the window has no
        running activity in it at all — absent data must not read as a zero week."""
        matching = [a.distance_km for a in self.activities
                    if a.sport == RUNNING_SPORT
                    and a.day.isocalendar()[:2] == (iso_year, iso_week)]
        return sum(matching) if matching else None

    @property
    def missing_metrics(self) -> tuple[str, ...]:
        names = ("chronic_load", "acute_load", "vo2max", "resting_hr", "hrv_status")
        return tuple(n for n in names if getattr(self, n) is None)


@dataclass(frozen=True)
class Step:
    """One line of intervals.icu workout-builder syntax, stored verbatim.

    Deliberately not decomposed into duration/distance/target: nothing in this system
    reasons about a step numerically (the guardrails use `Workout.total_km`), and a
    second representation would be a second syntax to keep in sync with theirs.
    `depth` is the nesting level, so a repeat block can be framed on output.
    """

    text: str
    depth: int = 0


@dataclass(frozen=True)
class Workout:
    day: date
    name: str
    sport: str
    steps: tuple[Step, ...]
    total_km: float
    is_quality: bool


@dataclass(frozen=True)
class Week:
    iso_year: int
    iso_week: int
    workouts: tuple[Workout, ...]

    @property
    def total_km(self) -> float:
        return sum(w.total_km for w in self.workouts)

    @property
    def quality_days(self) -> tuple[date, ...]:
        return tuple(w.day for w in self.workouts if w.is_quality)

    @property
    def longest_run_km(self) -> float:
        return max((w.total_km for w in self.workouts), default=0.0)


@dataclass(frozen=True)
class Plan:
    goal: str
    weeks: tuple[Week, ...]


class Severity(Enum):
    """Whether a violation stops the week or flags it.

    WARN exists only in the band between the nominal threshold and the hard one. A rule
    that is categorical, or that fires because the data is missing, is always BLOCK: an
    advisory fail-closed fallback would be no fallback at all.
    """

    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str
    severity: Severity = Severity.BLOCK


def next_iso_week(reference: date) -> tuple[int, int]:
    """ISO (year, week) of the week following `reference`.

    Derived from the date seven days later, never by incrementing the week number:
    ISO years have 52 or 53 weeks and the boundary does not follow the calendar year.
    """
    iso_year, iso_week, _ = (reference + timedelta(days=7)).isocalendar()
    return iso_year, iso_week
