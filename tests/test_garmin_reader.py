import json
from datetime import date

import pytest
from pathlib import Path

from smart_coach.garmin_reader import fetch_snapshot

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "garmin_activities.json").read_text())


class FakeGarmin:
    """Stands in for the garminconnect client. Returns recorded shapes, never network."""

    def __init__(self, *, training_status=None, vo2max=None, rhr=None, hrv=None,
                 profile=None, zones=None):
        self._training_status = training_status
        self._vo2max = vo2max
        self._rhr = rhr
        self._hrv = hrv
        self._profile = profile
        self._zones = zones

    def get_activities_by_date(self, start, end):
        return FIXTURE["activities"]

    def get_training_status(self, day):
        return self._training_status

    def get_max_metrics(self, day):
        return self._vo2max

    def get_rhr_day(self, day):
        return self._rhr

    def get_hrv_data(self, day):
        return self._hrv

    def get_user_profile(self):
        return self._profile

    def get_heart_rate_zones(self):
        return self._zones


def test_distances_convert_from_metres_to_kilometres():
    snapshot = fetch_snapshot(FakeGarmin(), as_of=date(2026, 9, 26), days=28)
    running = [a for a in snapshot.activities if a.sport == "running"]
    assert running[0].distance_km == 8.05  # 8046.7 m, not 8046.7 km


def test_trail_running_counts_as_running_and_cycling_does_not():
    snapshot = fetch_snapshot(FakeGarmin(), as_of=date(2026, 9, 26), days=28)
    sports = {a.sport for a in snapshot.activities}
    assert sports == {"running", "cycling", "walking"}
    assert snapshot.running_km == 8.05 + 12.5


def test_missing_metrics_become_none_not_zero():
    """A missing metric and a zero metric lead to opposite decisions: never conflate."""
    snapshot = fetch_snapshot(FakeGarmin(), as_of=date(2026, 9, 26), days=28)
    assert snapshot.chronic_load is None
    assert snapshot.acute_load is None
    assert snapshot.vo2max is None
    assert set(snapshot.missing_metrics) >= {"chronic_load", "acute_load", "vo2max"}


def test_present_metrics_are_extracted():
    client = FakeGarmin(
        training_status={"mostRecentTrainingLoadBalance": {"metricsTrainingLoadBalanceDTOMap": {
            "device": {"monthlyLoadAerobicLow": 0, "chronicTrainingLoad": 412.5,
                       "acuteTrainingLoad": 455.0}}}},
        # Observed shape: get_max_metrics returns a LIST with one entry, not a dict.
        vo2max=[{"userId": 1, "generic": {"vo2MaxPreciseValue": 51.4,
                                          "vo2MaxValue": 51.0}}],
        rhr={"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 48}]}}},
        hrv={"hrvSummary": {"status": "BALANCED"}},
    )
    snapshot = fetch_snapshot(client, as_of=date(2026, 9, 26), days=28)
    assert snapshot.chronic_load == 412.5
    assert snapshot.acute_load == 455.0
    assert snapshot.vo2max == 51.4
    assert snapshot.resting_hr == 48
    assert snapshot.hrv_status == "BALANCED"


# --- load derived from activities, because the watch does not compute it -------------

VENU_LIKE = dict(
    profile={"userData": {"gender": "MALE"}},
    zones=[{"maxHeartRateUsed": 185}],
    rhr={"allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [{"value": 50}]}}},
)


def test_load_is_derived_when_the_watch_does_not_report_it():
    """Both fixture runs fall inside the last 7 days, so the 7-day and 28-day means
    differ by exactly 28/7 — a relation that pins the windows, not just the numbers."""
    snapshot = fetch_snapshot(FakeGarmin(**VENU_LIKE), as_of=date(2026, 9, 26), days=28)

    assert snapshot.chronic_load is not None
    assert snapshot.acute_load is not None
    assert snapshot.acute_load / snapshot.chronic_load == pytest.approx(4.0)
    assert "chronic_load" not in snapshot.missing_metrics


def test_load_stays_none_when_the_profile_constants_are_unavailable():
    """Fail closed: no max heart rate means no TRIMP, and the guardrails fall back."""
    snapshot = fetch_snapshot(FakeGarmin(rhr=VENU_LIKE["rhr"]),
                              as_of=date(2026, 9, 26), days=28)
    assert snapshot.chronic_load is None
    assert snapshot.acute_load is None


def test_history_is_fetched_over_a_longer_window_than_the_load():
    """days_of_data must be able to exceed the load window, or the regime is capped."""
    asked = {}

    class Recording(FakeGarmin):
        def get_activities_by_date(self, start, end):
            asked["start"], asked["end"] = start, end
            return FIXTURE["activities"]

    fetch_snapshot(Recording(**VENU_LIKE), as_of=date(2026, 9, 26), days=28,
                   history_days=180)
    assert asked["start"] == "2026-03-30"


# --- daily metrics are missing until the watch syncs ---------------------------------

class SyncLagging(FakeGarmin):
    """Today's wellness data has not arrived yet, which is the normal state until the
    watch syncs. Yesterday's is there."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rhr_calls = []

    def get_rhr_day(self, day):
        self.rhr_calls.append(day)
        if day == "2026-09-26":
            return {"allMetrics": {"metricsMap": {
                "WELLNESS_RESTING_HEART_RATE": [{"value": 48}]}}}
        return None

    def get_max_metrics(self, day):
        return [{"generic": {"vo2MaxPreciseValue": 49.5}}] if day == "2026-09-25" else None

    def get_hrv_data(self, day):
        return {"hrvSummary": {"status": "BALANCED"}} if day == "2026-09-24" else None


def test_daily_metrics_fall_back_to_the_most_recent_day_that_has_them():
    """Run the command in the morning and today has nothing yet. Reading only `as_of`
    drops resting HR, and with it the whole load computation, without a word."""
    snapshot = fetch_snapshot(
        SyncLagging(profile=VENU_LIKE["profile"], zones=VENU_LIKE["zones"]),
        as_of=date(2026, 9, 27), days=28)

    assert snapshot.resting_hr == 48
    assert snapshot.vo2max == 49.5
    assert snapshot.hrv_status == "BALANCED"
    assert snapshot.missing_metrics == ()


def test_the_lookback_stops_at_the_first_day_with_a_value():
    """Garmin rate-limits by IP; walking further back than needed spends calls we may
    not get back."""
    client = SyncLagging()
    fetch_snapshot(client, as_of=date(2026, 9, 27), days=28)

    assert client.rhr_calls == ["2026-09-27", "2026-09-26"]


def test_a_metric_absent_for_the_whole_lookback_stays_none():
    snapshot = fetch_snapshot(FakeGarmin(), as_of=date(2026, 9, 27), days=28)
    assert snapshot.resting_hr is None
    assert "resting_hr" in snapshot.missing_metrics


def test_an_activity_carries_where_it_was_run_and_how_much_it_climbed():
    """Pace and heart rate on a hilly route abroad are not comparable with the same
    numbers at home: without these two fields the coach reads a hard month as an
    unfit athlete."""
    from smart_coach.garmin_reader import _to_activity

    hilly = _to_activity({"startTimeLocal": "2026-09-02 07:30:00",
                          "activityType": {"typeKey": "running"}, "distance": 8000.0,
                          "duration": 2896.0, "averageHR": 150,
                          "locationName": "Hillside", "elevationGain": 140.4})
    assert (hilly.location, hilly.elevation_gain_m) == ("Hillside", 140.4)


def test_an_activity_without_route_data_reports_it_as_missing_not_as_flat():
    from smart_coach.garmin_reader import _to_activity

    indoor = _to_activity({"startTimeLocal": "2026-09-02 07:30:00",
                           "activityType": {"typeKey": "treadmill_running"},
                           "distance": 5000.0, "duration": 1800.0, "averageHR": 140})
    assert (indoor.location, indoor.elevation_gain_m) == (None, None)
