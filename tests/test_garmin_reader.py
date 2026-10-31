import json
from datetime import date

import pytest
import requests
from pathlib import Path

from smart_coach import garmin_reader
from smart_coach.config import MissingSettingError
from smart_coach.garmin_reader import (ACTIVITY_PAGE_SIZE, GarminAuthError, GarminReadError,
                                       PersonalApiClient, fetch_snapshot)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "garmin_activities.json").read_text())


class FakeGarmin:
    """Stands in for the Personal API client. Returns recorded shapes, never network."""

    def __init__(self, *, rhr=None, hrv=None, zones=None):
        self._rhr = rhr
        self._hrv = hrv
        self._zones = zones

    def activities(self, start, end):
        return FIXTURE["activities"]

    def daily_heart_rate(self, day):
        return self._rhr

    def hrv(self, day):
        return self._hrv

    def heart_rate_zones(self):
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
    assert snapshot.resting_hr is None
    assert set(snapshot.missing_metrics) == {"chronic_load", "acute_load", "resting_hr",
                                             "hrv_status"}


def test_present_metrics_are_extracted():
    client = FakeGarmin(rhr={"restingHeartRate": 48, "maxHeartRate": 170},
                        hrv={"hrvSummary": {"status": "BALANCED"}})
    snapshot = fetch_snapshot(client, as_of=date(2026, 9, 26), days=28)
    assert snapshot.resting_hr == 48
    assert snapshot.hrv_status == "BALANCED"


# --- load derived from activities: Garmin's own is not exposed -----------------------

VENU_LIKE = dict(
    zones=[{"sport": "DEFAULT", "maxHeartRateUsed": 185}],
    rhr={"restingHeartRate": 50},
)


def test_load_is_derived_from_the_activities():
    """Both fixture runs fall inside the last 7 days, so the 7-day and 28-day means
    differ by exactly 28/7 — a relation that pins the windows, not just the numbers."""
    snapshot = fetch_snapshot(FakeGarmin(**VENU_LIKE), as_of=date(2026, 9, 26), days=28,
                              is_male=True)

    assert snapshot.chronic_load is not None
    assert snapshot.acute_load is not None
    assert snapshot.acute_load / snapshot.chronic_load == pytest.approx(4.0)
    assert "chronic_load" not in snapshot.missing_metrics


def test_load_stays_none_when_the_profile_constants_are_unavailable():
    """Fail closed: no max heart rate means no TRIMP, and the guardrails fall back."""
    snapshot = fetch_snapshot(FakeGarmin(rhr=VENU_LIKE["rhr"]),
                              as_of=date(2026, 9, 26), days=28, is_male=True)
    assert snapshot.chronic_load is None
    assert snapshot.acute_load is None


def test_load_stays_none_when_the_athlete_has_not_declared_their_sex():
    snapshot = fetch_snapshot(FakeGarmin(**VENU_LIKE), as_of=date(2026, 9, 26), days=28)
    assert (snapshot.acute_load, snapshot.chronic_load) == (None, None)


def _chronic_with(zones) -> float | None:
    client = FakeGarmin(rhr=VENU_LIKE["rhr"], zones=zones)
    return fetch_snapshot(client, as_of=date(2026, 9, 26), days=28, is_male=True).chronic_load


def test_the_running_zones_win_over_the_default_ones():
    """The athlete may have set a running-specific max heart rate; it is the one their
    runs were measured against."""
    running = {"sport": "RUNNING", "maxHeartRateUsed": 185}
    default = {"sport": "DEFAULT", "maxHeartRateUsed": 200}
    assert _chronic_with([default, running]) == _chronic_with([running])
    assert _chronic_with([default, running]) != _chronic_with([default])


def test_history_is_fetched_over_a_longer_window_than_the_load():
    """days_of_data must be able to exceed the load window, or the regime is capped."""
    asked = {}

    class Recording(FakeGarmin):
        def activities(self, start, end):
            asked["start"], asked["end"] = start, end
            return FIXTURE["activities"]

    snapshot = fetch_snapshot(Recording(**VENU_LIKE), as_of=date(2026, 9, 26), days=28,
                              history_days=180)
    assert asked["start"] == "2026-03-30"
    assert snapshot.history_days == 180


# --- daily metrics are missing until the watch syncs ---------------------------------

class SyncLagging(FakeGarmin):
    """Today's wellness data has not arrived yet, which is the normal state until the
    watch syncs. Yesterday's is there."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.rhr_calls = []

    def daily_heart_rate(self, day):
        self.rhr_calls.append(day)
        return {"restingHeartRate": 48} if day == "2026-09-26" else {}

    def hrv(self, day):
        return {"hrvSummary": {"status": "BALANCED"}} if day == "2026-09-24" else None


def test_daily_metrics_fall_back_to_the_most_recent_day_that_has_them():
    """Run the command in the morning and today has nothing yet. Reading only `as_of`
    drops resting HR, and with it the whole load computation, without a word."""
    snapshot = fetch_snapshot(SyncLagging(zones=VENU_LIKE["zones"]),
                              as_of=date(2026, 9, 27), days=28, is_male=True)

    assert snapshot.resting_hr == 48
    assert snapshot.hrv_status == "BALANCED"
    assert snapshot.missing_metrics == ()


def test_the_lookback_stops_at_the_first_day_with_a_value():
    """Calls we do not need are calls a rate limit may not give back later."""
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


# --- the HTTP client -----------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text if text is not None else ("" if body is None else json.dumps(body))

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code} Client Error")


class FakeSession:
    """Answers GETs from a queue and records what was asked."""

    def __init__(self, *responses, token=FakeResponse(text="a.jwt.token")):
        self._responses = list(responses)
        self._token = token
        self.headers = {}
        self.gets, self.posts = [], []

    def get(self, url, params=None, timeout=None):
        self.gets.append((url, params))
        return self._responses.pop(0)

    def post(self, url, headers=None, timeout=None):
        self.posts.append((url, headers))
        if isinstance(self._token, Exception):
            raise self._token
        return self._token


def test_activities_are_read_page_by_page_until_a_short_page():
    """Garmin returns at most one page per call: stopping after the first would silently
    drop the oldest runs, and with them the athlete's history."""
    run = FIXTURE["activities"][0]
    session = FakeSession(FakeResponse(body=[run] * ACTIVITY_PAGE_SIZE),
                          FakeResponse(body=[run]))
    found = PersonalApiClient(session).activities("2026-04-01", "2026-09-27")

    assert len(found) == ACTIVITY_PAGE_SIZE + 1
    assert [params["start"] for _, params in session.gets] == [0, ACTIVITY_PAGE_SIZE]
    assert session.gets[0][1]["startDate"] == "2026-04-01"


def test_a_day_without_data_reads_as_none():
    """Garmin answers 204 with no body for a night without an HRV reading."""
    session = FakeSession(FakeResponse(status_code=204, text=""))
    assert PersonalApiClient(session).hrv("2026-09-27") is None


def test_a_refused_read_fails_the_whole_snapshot_under_its_own_name():
    session = FakeSession(FakeResponse(status_code=429))
    with pytest.raises(GarminReadError, match="429"):
        fetch_snapshot(PersonalApiClient(session), date(2026, 9, 27), 28)


@pytest.fixture
def session(monkeypatch):
    """The session build_client opens, so the test can see what it sent."""
    def open_session(token=FakeResponse(text="a.jwt.token")):
        opened = FakeSession(token=token)
        monkeypatch.setattr(garmin_reader.requests, "Session", lambda: opened)
        return opened
    monkeypatch.setenv("GARMIN_API_KEY", "the-athletes-key")
    return open_session


def test_the_key_is_exchanged_for_a_bearer_token(session):
    opened = session()
    garmin_reader.build_client()

    assert opened.posts[0][1]["Authorization"] == "ApiKey the-athletes-key"
    assert opened.headers["Authorization"] == "Bearer a.jwt.token"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_says_what_to_do_and_never_echoes_the_key(session, status):
    session(token=FakeResponse(status_code=status, text="denied"))

    with pytest.raises(GarminAuthError) as caught:
        garmin_reader.build_client()
    message = str(caught.value)
    assert "GARMIN_API_KEY" in message and "Personal API" in message
    assert "the-athletes-key" not in message


def test_an_empty_token_is_an_auth_error_not_a_later_read_failure(session):
    """Sent as `Bearer `, it would fail every read with a 401 that points at the data."""
    session(token=FakeResponse(text="  \n"))

    with pytest.raises(GarminAuthError, match="empty token"):
        garmin_reader.build_client()


def test_garmin_unreachable_is_an_auth_error_not_a_library_traceback(session):
    session(token=requests.ConnectionError("no route to host"))

    with pytest.raises(GarminAuthError, match="ConnectionError"):
        garmin_reader.build_client()


def test_a_missing_key_is_reported_by_name(monkeypatch):
    monkeypatch.delenv("GARMIN_API_KEY", raising=False)
    with pytest.raises(MissingSettingError, match="GARMIN_API_KEY"):
        garmin_reader.build_client()
