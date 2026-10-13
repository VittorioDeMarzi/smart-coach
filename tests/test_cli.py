from datetime import date

from smart_coach.cli import validate_command
from smart_coach.models import Regime, Snapshot, Step, Week, Workout

SNAPSHOT = Snapshot(as_of=date(2026, 9, 27), activities=(), chronic_load=None,
                    acute_load=None, vo2max=None, resting_hr=None, hrv_status=None)


def _week(total_km: float) -> Week:
    """Long run plus three equal easy runs: a plausible distribution, so the long-run
    share rule does not fire on an otherwise admissible week."""
    longest = total_km * 0.3
    each = (total_km - longest) / 3
    days = (date(2026, 9, 28), date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 4))
    return Week(iso_year=2026, iso_week=40, workouts=tuple(
        Workout(day=day, name=name, sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=km, is_quality=False)
        for day, name, km in zip(days, ("Long", "Easy", "Easy", "Easy"),
                                 (longest, each, each, each))
    ))


def test_validate_returns_zero_for_an_admissible_week(capsys):
    code = validate_command(_week(20.0), None, SNAPSHOT, Regime.COLD,
                            config_path="tests/fixtures/config.toml")
    assert code == 0
    assert "OK" in capsys.readouterr().out


def test_validate_returns_one_and_lists_violations(capsys):
    # 60 km with load data missing exceeds the fallback cap of 45 km.
    code = validate_command(_week(60.0), None, SNAPSHOT, Regime.COLD,
                            config_path="tests/fixtures/config.toml")
    out = capsys.readouterr().out
    assert code == 1
    assert "absolute_volume_cap" in out


def test_push_is_refused_when_the_week_violates_a_guardrail(monkeypatch, capsys):
    """A violation blocks the push. No silent correction (spec 7.3)."""
    from smart_coach import cli

    pushed = []
    monkeypatch.setattr(cli, "push_week", lambda *a, **k: pushed.append(a))
    code = cli.push_command(_week(60.0), None, SNAPSHOT, Regime.COLD,
                            athlete_id="i1", api_key="k", config_path="tests/fixtures/config.toml")

    assert code == 1
    assert pushed == []
    assert "refused" in capsys.readouterr().out.lower()


def test_the_snapshot_lists_recent_runs_with_pace_place_and_climb():
    from smart_coach import cli
    from smart_coach.models import Activity

    runs = (
        Activity(day=date(2026, 9, 2), sport="running", distance_km=8.0,
                 duration_s=2896, avg_hr=150, location="Hillside", elevation_gain_m=140.4),
        Activity(day=date(2026, 9, 20), sport="cycling", distance_km=40.0,
                 duration_s=5400, avg_hr=120),
        Activity(day=date(2026, 9, 24), sport="running", distance_km=10.0,
                 duration_s=3226, avg_hr=None),
        Activity(day=date(2026, 7, 1), sport="running", distance_km=5.0,
                 duration_s=1500, avg_hr=140),
    )
    snapshot = Snapshot(as_of=date(2026, 9, 27), activities=runs, chronic_load=None,
                        acute_load=None, vo2max=None, resting_hr=None, hrv_status=None)
    assert cli.recent_runs_report(snapshot) == [
        "2026-09-02   8.0 km  6:02/km  HR 150  climb 140 m  Hillside",
        "2026-09-24  10.0 km  5:23/km  HR   ?  climb   ? m  ?",
    ]
