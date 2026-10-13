"""Training load computed from what the watch actually records.

The Venu 3 does not produce acute/chronic load, so we derive it ourselves with Banister's
TRIMP: duration x heart-rate-reserve fraction x an exponential weighting that differs by
sex. Reference values below are the formula applied by hand to the stated inputs.
"""

from datetime import date, timedelta

import pytest

from smart_coach.load import AthleteProfile, acute_chronic_load, trimp
from smart_coach.models import Activity

MALE = AthleteProfile(resting_hr=50, max_hr=185, is_male=True)
FEMALE = AthleteProfile(resting_hr=50, max_hr=185, is_male=False)


def test_trimp_matches_the_banister_formula():
    assert trimp(duration_s=3600, avg_hr=150, profile=MALE) == pytest.approx(117.94, abs=0.01)


def test_trimp_scales_linearly_with_duration():
    assert trimp(duration_s=1800, avg_hr=150, profile=MALE) == pytest.approx(58.97, abs=0.01)


def test_trimp_grows_faster_than_linearly_with_intensity():
    easy = trimp(duration_s=3600, avg_hr=120, profile=MALE)
    hard = trimp(duration_s=3600, avg_hr=150, profile=MALE)
    assert easy == pytest.approx(53.88, abs=0.01)
    assert hard / easy > 2.0, "the exponential weighting is the point of TRIMP"


def test_the_female_coefficient_differs_from_the_male_one():
    """0.86e^1.67x, not 0.64e^1.92x. Several popular sites get this wrong."""
    assert trimp(duration_s=3600, avg_hr=150, profile=FEMALE) == pytest.approx(131.69, abs=0.01)


def test_heart_rate_at_rest_produces_no_load():
    assert trimp(duration_s=3600, avg_hr=50, profile=MALE) == 0.0


def test_an_unusable_profile_yields_no_load():
    """Fail closed: a missing constant must not silently become a zero load."""
    assert trimp(duration_s=3600, avg_hr=150,
                 profile=AthleteProfile(resting_hr=None, max_hr=185, is_male=True)) is None
    assert trimp(duration_s=3600, avg_hr=150,
                 profile=AthleteProfile(resting_hr=50, max_hr=None, is_male=True)) is None
    assert trimp(duration_s=3600, avg_hr=None, profile=MALE) is None


def test_max_below_resting_is_refused_rather_than_producing_a_negative_load():
    bad = AthleteProfile(resting_hr=190, max_hr=185, is_male=True)
    assert trimp(duration_s=3600, avg_hr=150, profile=bad) is None


def _run(day: date, minutes: int, hr: int) -> Activity:
    return Activity(day=day, sport="running", distance_km=10.0,
                    duration_s=minutes * 60, avg_hr=hr)


def test_acute_and_chronic_are_daily_averages_over_seven_and_twentyeight_days():
    as_of = date(2026, 9, 28)
    # One identical session every day for 28 days: both averages must agree.
    activities = tuple(_run(as_of - timedelta(days=n), 60, 150) for n in range(28))
    acute, chronic = acute_chronic_load(activities, as_of, MALE)
    assert acute == pytest.approx(117.94, abs=0.01)
    assert chronic == pytest.approx(117.94, abs=0.01)


def test_a_recent_ramp_pushes_the_ratio_above_one():
    as_of = date(2026, 9, 28)
    hard_week = tuple(_run(as_of - timedelta(days=n), 90, 155) for n in range(7))
    quiet_before = tuple(_run(as_of - timedelta(days=n), 30, 130) for n in range(7, 28))
    acute, chronic = acute_chronic_load(hard_week + quiet_before, as_of, MALE)
    assert acute / chronic > 1.3, "a hard week after three easy ones must trip the band"


def test_non_running_activities_do_not_contribute():
    as_of = date(2026, 9, 28)
    runs = tuple(_run(as_of - timedelta(days=n), 60, 150) for n in range(28))
    ride = Activity(day=as_of, sport="cycling", distance_km=80.0,
                    duration_s=10800, avg_hr=140)

    assert acute_chronic_load(runs, as_of, MALE) == acute_chronic_load(runs + (ride,),
                                                                      as_of, MALE)


def test_only_cycling_yields_none_because_running_load_is_unknown():
    """Not zero: no runs recorded means "cannot say", not "rested". A zero chronic load
    would make the ratio meaningless exactly when the athlete is least characterised."""
    as_of = date(2026, 9, 28)
    ride = Activity(day=as_of, sport="cycling", distance_km=80.0,
                    duration_s=10800, avg_hr=140)
    assert acute_chronic_load((ride,), as_of, MALE) == (None, None)


def test_no_usable_activity_yields_none_not_zero():
    """None means "cannot say"; zero means "rested". The guardrails treat them oppositely."""
    acute, chronic = acute_chronic_load((), date(2026, 9, 28), MALE)
    assert acute is None and chronic is None


def test_activities_without_heart_rate_are_reported_as_unusable():
    as_of = date(2026, 9, 28)
    no_hr = tuple(Activity(day=as_of - timedelta(days=n), sport="running",
                           distance_km=10.0, duration_s=3600, avg_hr=None)
                  for n in range(28))
    acute, chronic = acute_chronic_load(no_hr, as_of, MALE)
    assert acute is None and chronic is None
