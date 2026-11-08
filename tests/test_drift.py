"""Heart-rate drift. No real FIT file here: it holds the athlete's GPS track, so the
decoder output is built by hand in the shape Garmin's files have."""

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from smart_coach import drift
from smart_coach.drift import (HR_SETTLE_S, MIN_DRIFT_DURATION_S, FitFileError,
                               decoupling, read_fit, run_drifts, steady_samples)
from smart_coach.models import Activity, Sample

START = datetime(2026, 11, 8, 12, 16, 43, tzinfo=timezone.utc)
RUN_S = 3300


def _samples(heart_rate=lambda t: 140, speed=lambda t: 2.9, seconds=RUN_S):
    return tuple(Sample(elapsed_s=float(t), speed_mps=speed(t), heart_rate=heart_rate(t))
                 for t in range(seconds))


# --- decoupling -------------------------------------------------------------------

def test_constant_speed_and_heart_rate_has_no_drift():
    assert decoupling(_samples()) == pytest.approx(0.0)


def test_heart_rate_rising_five_percent_at_constant_speed():
    middle = (HR_SETTLE_S + RUN_S - 1) / 2
    samples = _samples(heart_rate=lambda t: 140 if t < middle else 147)
    assert decoupling(samples) == pytest.approx((1 - 140 / 147) * 100)


def test_stops_are_left_out():
    """Standing at a light with a falling heart rate would otherwise fake an
    improvement in the half it falls in."""
    stopped = range(2500, 2600)
    samples = _samples(speed=lambda t: 0.0 if t in stopped else 2.9,
                       heart_rate=lambda t: 100 if t in stopped else 140)
    assert decoupling(samples) == pytest.approx(0.0)


def test_the_first_minutes_are_ignored():
    samples = _samples(heart_rate=lambda t: 110 if t < HR_SETTLE_S else 140)
    assert decoupling(samples) == pytest.approx(0.0)


def test_too_few_heart_rate_samples_is_none_not_zero():
    samples = _samples(heart_rate=lambda t: 140 if t % 2 else None)
    assert decoupling(samples) is None


def test_no_speed_at_all_is_none():
    """A treadmill without a footpod records heart rate and nothing else."""
    assert decoupling(_samples(speed=lambda t: None)) is None


# --- reading the file -------------------------------------------------------------

def _record(t, heart_rate=140):
    return {"timestamp": START + timedelta(seconds=t), "enhanced_speed": 2.9,
            "heart_rate": heart_rate}


def _lap(start_s, length_s, intensity, step=None):
    return {"start_time": START + timedelta(seconds=start_s),
            "total_elapsed_time": float(length_s), "intensity": intensity,
            "wkt_step_index": step}


def _zip(name="25008957306_ACTIVITY.fit"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, b"not decoded: the decoder is patched")
    return buffer.getvalue()


@pytest.fixture
def decoded(monkeypatch):
    """Stand in for the FIT SDK with the messages a test gives."""
    def use(messages):
        monkeypatch.setattr(drift, "_decode", lambda fit: messages)
    return use


def test_warm_up_and_cool_down_laps_are_left_out(decoded):
    decoded({"record_mesgs": [_record(t) for t in range(900)],
             "lap_mesgs": [_lap(0, 300, "warmup"), _lap(300, 300, "active"),
                           _lap(600, 300, "cooldown")]})
    kept = steady_samples(read_fit(_zip()))
    assert (kept[0].elapsed_s, kept[-1].elapsed_s) == (300.0, 599.0)


def test_a_file_without_intensity_keeps_every_record(decoded):
    decoded({"record_mesgs": [_record(t) for t in range(900)],
             "lap_mesgs": [_lap(0, 900, None)]})
    assert len(steady_samples(read_fit(_zip()))) == 900


def test_a_free_run_is_continuous_and_a_strides_session_is_not(decoded):
    decoded({"record_mesgs": [_record(0)],
             "lap_mesgs": [_lap(0, 360, "interval"), _lap(360, 360, "interval")]})
    assert read_fit(_zip()).is_continuous
    decoded({"record_mesgs": [_record(0)],
             "lap_mesgs": [_lap(0, 2000, "active", 0), _lap(2000, 20, "active", 1),
                           _lap(2020, 70, "recovery", 2)]})
    assert not read_fit(_zip()).is_continuous


def test_one_workout_step_between_warm_up_and_cool_down_is_continuous(decoded):
    decoded({"record_mesgs": [_record(0)],
             "lap_mesgs": [_lap(0, 700, "warmup", 0), _lap(700, 3600, "active", 1),
                           _lap(4300, 600, "cooldown", 2)]})
    assert read_fit(_zip()).is_continuous


def test_a_fast_finish_is_not_continuous(decoded):
    """No recovery lap, but a second step at a faster pace by design."""
    decoded({"record_mesgs": [_record(0)],
             "lap_mesgs": [_lap(0, 4000, "active", 0), _lap(4000, 900, "active", 1)]})
    assert not read_fit(_zip()).is_continuous


def test_a_download_without_a_fit_file_is_refused():
    with pytest.raises(FitFileError):
        read_fit(_zip(name="summary.json"))


def test_a_lap_without_times_is_refused(decoded):
    decoded({"record_mesgs": [_record(0)],
             "lap_mesgs": [{"start_time": START, "intensity": "active"}]})
    with pytest.raises(FitFileError):
        read_fit(_zip())


# --- one download per run ---------------------------------------------------------

class FakeClient:
    """Counts downloads; answers with the given body, or raises it."""

    def __init__(self, body):
        self.body = body
        self.downloads = []

    def activity_fit(self, activity_id):
        self.downloads.append(activity_id)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _run(activity_id, duration_s=MIN_DRIFT_DURATION_S, day=8):
    return Activity(day=date(2026, 11, day), sport="running", distance_km=9.6,
                    duration_s=duration_s, avg_hr=140, activity_id=activity_id)


def test_a_short_run_is_not_downloaded():
    client = FakeClient(_zip())
    assert run_drifts(client, (_run(1, duration_s=MIN_DRIFT_DURATION_S - 1),)) == {}
    assert client.downloads == []


def test_a_run_without_an_id_is_not_measured():
    client = FakeClient(_zip())
    assert run_drifts(client, (_run(None),)) == {}
    assert client.downloads == []


def test_a_continuous_run_gets_its_drift(decoded):
    decoded({"record_mesgs": [_record(t) for t in range(RUN_S)],
             "lap_mesgs": [_lap(0, RUN_S, "interval")]})
    assert run_drifts(FakeClient(_zip()), (_run(1),)) == {1: pytest.approx(0.0)}


def test_a_run_with_recoveries_is_not_measured(decoded):
    decoded({"record_mesgs": [_record(t) for t in range(RUN_S)],
             "lap_mesgs": [_lap(0, 3000, "active"), _lap(3000, 300, "recovery")]})
    assert run_drifts(FakeClient(_zip()), (_run(1),)) == {}


def test_a_failed_download_is_none_for_that_run_only(decoded, capsys):
    decoded({"record_mesgs": [_record(t) for t in range(RUN_S)], "lap_mesgs": []})

    class FailingOnce(FakeClient):
        def activity_fit(self, activity_id):
            if activity_id == 1:
                raise requests.HTTPError("500 Server Error: the-body")
            return super().activity_fit(activity_id)

    drifts = run_drifts(FailingOnce(_zip()), (_run(1, day=5), _run(2)))

    assert drifts == {1: None, 2: pytest.approx(0.0)}
    err = capsys.readouterr().err
    assert "HTTPError" in err and "the-body" not in err


def test_a_malformed_file_is_none_and_does_not_stop_the_snapshot(decoded):
    decoded({"record_mesgs": [_record(0)], "lap_mesgs": [{"intensity": "active"}]})
    assert run_drifts(FakeClient(_zip()), (_run(1),)) == {1: None}
