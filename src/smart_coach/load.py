"""Training load derived from recorded activities.

Lifestyle Garmin watches (the Venu series among them) do not compute acute and chronic
training load — those are Forerunner/Fenix features. Everything the computation needs is
in the activities we already read: duration and average heart rate.

The method is Banister's TRIMP:

    TRIMP = minutes x hrr x Y(hrr)
    hrr   = (avg HR - resting HR) / (max HR - resting HR)
    Y     = 0.64 * e^(1.92 * hrr)   for men
            0.86 * e^(1.67 * hrr)   for women

Note the female coefficient is 0.86/1.67, not 0.64 with a different exponent: several
popular reproductions get that wrong.

Every function returns None rather than a number it cannot justify. None means "cannot
say" and routes the guardrails to their conservative fallback; zero means "rested" and
does not. Conflating them is the failure this whole design guards against.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from smart_coach.models import RUNNING_SPORT, Activity

ACUTE_DAYS = 7
CHRONIC_DAYS = 28
SECONDS_PER_MINUTE = 60.0
MALE_FACTOR, MALE_EXPONENT = 0.64, 1.92
FEMALE_FACTOR, FEMALE_EXPONENT = 0.86, 1.67


@dataclass(frozen=True)
class AthleteProfile:
    """The three constants TRIMP needs. Read from Garmin, not asked of the athlete."""

    resting_hr: int | None
    max_hr: int | None
    is_male: bool


def trimp(duration_s: int, avg_hr: float | None, profile: AthleteProfile) -> float | None:
    """Training impulse of one session, or None when it cannot be computed."""
    if avg_hr is None or profile.resting_hr is None or profile.max_hr is None:
        return None
    span = profile.max_hr - profile.resting_hr
    if span <= 0:
        return None
    reserve = (avg_hr - profile.resting_hr) / span
    if reserve <= 0:
        return 0.0
    factor, exponent = ((MALE_FACTOR, MALE_EXPONENT) if profile.is_male
                        else (FEMALE_FACTOR, FEMALE_EXPONENT))
    weighting = factor * math.exp(exponent * reserve)
    return duration_s / SECONDS_PER_MINUTE * reserve * weighting


def _load_in_window(
    activities: tuple[Activity, ...], as_of: date, days: int, profile: AthleteProfile
) -> float | None:
    """Mean daily TRIMP over the window.

    Zero when the window holds no run: that is rest, and it is known. None as soon as
    one run in it cannot be scored: leaving that run out would report a load lower than
    the truth without saying so, and a week of runs recorded without heart rate would
    read as a week off.
    """
    earliest = as_of - timedelta(days=days - 1)
    scores = [trimp(a.duration_s, a.avg_hr, profile)
              for a in activities
              if a.sport == RUNNING_SPORT and earliest <= a.day <= as_of]
    if any(score is None for score in scores):
        return None
    return sum(scores) / days


def acute_chronic_load(
    activities: tuple[Activity, ...], as_of: date, profile: AthleteProfile
) -> tuple[float | None, float | None]:
    """Acute (7-day) and chronic (28-day) mean daily load.

    Averages rather than totals, so their ratio is the acute:chronic workload ratio the
    guardrail band is expressed in.
    """
    running = tuple(a for a in activities if a.sport == RUNNING_SPORT)
    if not running:
        return None, None
    chronic = _load_in_window(activities, as_of, CHRONIC_DAYS, profile)
    if chronic is None:
        return None, None
    # The acute window lies inside the chronic one, so it is scoreable whenever that is.
    return _load_in_window(activities, as_of, ACUTE_DAYS, profile), chronic
