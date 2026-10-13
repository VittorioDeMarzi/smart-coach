"""Regression tests from the second review (2026-10-11). Each name carries its finding.

Every one reproduced a real defect before the fix: a race week refused, kilometres
already run left out of the count, runs without heart rate read as rest, and failures
reported under the wrong name or as a traceback.
"""

from datetime import date, timedelta

import pytest

from smart_coach import cli, garmin_reader
from smart_coach.config import ConfigError, MissingSettingError, load_config
from smart_coach.garmin_reader import GarminReadError, fetch_snapshot
from smart_coach.guardrails import (blocking, expected_week_km, recent_week_km,
                                    validate)
from smart_coach.load import AthleteProfile, acute_chronic_load
from smart_coach.models import Activity, Regime, Severity, Snapshot, Week, Workout
from smart_coach.plan_writer import PlanParseError, read_plan

CONFIG = load_config("tests/fixtures/config.toml")
MONDAY_W42 = date(2026, 10, 12)


def _week(iso_week: int, *day_km: tuple[int, float], iso_year: int = 2026) -> Week:
    """`day_km` are (weekday offset from Monday, kilometres)."""
    monday = date.fromisocalendar(iso_year, iso_week, 1)
    return Week(iso_year=iso_year, iso_week=iso_week, workouts=tuple(
        Workout(day=monday + timedelta(days=offset), name=f"Run {n}", sport="running",
                steps=(), total_km=km, is_quality=False)
        for n, (offset, km) in enumerate(day_km)))


def _ran(day: date, km: float, hr: int | None = 140) -> Activity:
    return Activity(day=day, sport="running", distance_km=km, duration_s=1800, avg_hr=hr)


def _snapshot(as_of: date, *activities: Activity, load: float | None = 400.0) -> Snapshot:
    return Snapshot(as_of=as_of, activities=activities, chronic_load=load,
                    acute_load=load, vo2max=None, resting_hr=None, hrv_status=None)


def _found(violations) -> dict:
    return {v.rule: v.severity for v in violations}


# --- R2: a race week is mostly its race ----------------------------------------------

def test_r2_a_race_week_is_flagged_for_its_shape_but_never_refused_for_it():
    """Two shakeouts and a half marathon: the race is 70% of the week and that is what
    a race week looks like. The share has no evidence behind it, so it only warns."""
    race_week = _week(13, (1, 5.0), (3, 4.0), (6, 21.1), iso_year=2027)
    snapshot = _snapshot(date(2027, 3, 28),
                         *(_ran(date(2027, 3, 28) - timedelta(days=d), km)
                           for d, km in ((14, 19.0), (12, 8.0), (10, 8.0), (7, 16.0),
                                         (5, 8.0), (3, 6.0), (21, 18.0), (19, 9.0))))
    violations = validate(race_week, None, snapshot, Regime.HOT, CONFIG)
    assert _found(violations)["long_run_share"] is Severity.WARN
    assert "long_run_share" not in _found(blocking(violations))


# --- R3: a week already under way ----------------------------------------------------

FOUR_STEADY_WEEKS = tuple(_ran(MONDAY_W42 - timedelta(days=7 * k + 4), 17.5)
                          for k in range(4))


def test_r3_kilometres_already_run_this_week_count_towards_it():
    """Ten kilometres on Monday, a plan written on Wednesday holding only the rest:
    the week is 29 km, not the 19 on the page."""
    wednesday = MONDAY_W42 + timedelta(days=2)
    rest_of_week = _week(42, (3, 8.0), (6, 11.0))
    snapshot = _snapshot(wednesday, _ran(MONDAY_W42, 10.0), *FOUR_STEADY_WEEKS)
    assert expected_week_km(rest_of_week, snapshot) == 29.0
    assert _found(validate(rest_of_week, None, snapshot, Regime.HOT, CONFIG)).get(
        "weekly_volume_increase") is Severity.BLOCK


def test_r3_a_week_entirely_ahead_is_worth_exactly_what_is_planned():
    week = _week(42, (1, 6.0), (3, 6.0), (6, 7.0))
    sunday_before = MONDAY_W42 - timedelta(days=1)
    assert expected_week_km(week, _snapshot(sunday_before, _ran(sunday_before, 12.0))) == 19.0


def test_r3_todays_session_counts_whether_or_not_it_has_happened_yet():
    thursday = MONDAY_W42 + timedelta(days=3)
    week = _week(42, (3, 8.0), (6, 11.0))
    assert expected_week_km(week, _snapshot(thursday)) == 19.0
    assert expected_week_km(week, _snapshot(thursday, _ran(thursday, 9.5))) == 20.5


def test_r3_a_planned_session_whose_day_passed_unrun_counts_for_nothing():
    friday = MONDAY_W42 + timedelta(days=4)
    week = _week(42, (1, 6.0), (6, 11.0))
    assert expected_week_km(week, _snapshot(friday)) == 11.0


def test_r3_the_absolute_cap_also_sees_the_kilometres_already_run():
    wednesday = MONDAY_W42 + timedelta(days=2)
    rest_of_week = _week(42, (3, 12.0), (4, 12.0), (6, 16.0))
    snapshot = _snapshot(wednesday, _ran(MONDAY_W42, 10.0), load=None)
    messages = [v.message for v in validate(rest_of_week, None, snapshot, Regime.COLD,
                                            CONFIG) if v.rule == "absolute_volume_cap"]
    assert any("50.0 km" in message for message in messages)


def test_r3_two_planned_sessions_on_one_day_are_both_counted():
    double = Week(iso_year=2026, iso_week=42, workouts=tuple(
        Workout(day=MONDAY_W42 + timedelta(days=1), name=name, sport="running",
                steps=(), total_km=km, is_quality=False)
        for name, km in (("Morning", 6.0), ("Evening", 5.0))))
    sunday_before = MONDAY_W42 - timedelta(days=1)
    assert recent_week_km(_week(43, (1, 5.0)), (double,), _snapshot(sunday_before),
                          CONFIG)[0] == 11.0


def test_r3_a_session_dated_outside_its_week_heading_is_rejected(tmp_path):
    """The heading decides which week a session is judged in, the date decides when it
    is run. When they disagree one of the two is wrong, and guessing hides it."""
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W42\n\n"
                    "- 2026-10-13 | Easy | 6.0 km | easy\n"
                    "- 2026-10-20 | Long run | 12.0 km | easy\n")
    with pytest.raises(PlanParseError, match="2026-W42") as caught:
        read_plan(plan)
    assert caught.value.line_number == 8


# --- R4: a run without heart rate is not rest ----------------------------------------

PROFILE = AthleteProfile(resting_hr=50, max_hr=185, is_male=True)
TODAY = date(2026, 10, 11)


def test_r4_a_week_of_runs_without_heart_rate_does_not_read_as_a_week_of_rest():
    scored = tuple(_ran(TODAY - timedelta(days=d), 10.0) for d in (10, 14, 18, 22))
    unscored = tuple(_ran(TODAY - timedelta(days=d), 12.0, hr=None) for d in (1, 3, 5))
    assert acute_chronic_load(scored + unscored, TODAY, PROFILE) == (None, None)


def test_r4_one_unscored_run_makes_the_whole_window_unknown():
    """Dropping it would report a load lower than the truth without saying so."""
    runs = (_ran(TODAY - timedelta(days=2), 10.0),
            _ran(TODAY - timedelta(days=20), 10.0, hr=None))
    assert acute_chronic_load(runs, TODAY, PROFILE) == (None, None)


def test_r4_a_week_with_no_run_at_all_is_genuinely_zero():
    runs = tuple(_ran(TODAY - timedelta(days=d), 10.0) for d in (10, 14, 18))
    acute, chronic = acute_chronic_load(runs, TODAY, PROFILE)
    assert acute == 0.0
    assert chronic > 0


# --- R5: failures reported under their own name --------------------------------------

RULES_WITHOUT_GUARDRAILS = (
    "[regime]\ncold_max_days = 13\nwarm_max_days = 27\ndecay_threshold_weeks = 3\n\n"
    '[workout]\neasy_step_target = "Z2 Pace"\n')


def test_r5_a_config_without_a_section_names_the_section_and_the_file(tmp_path):
    (tmp_path / "config.toml").write_text(RULES_WITHOUT_GUARDRAILS)
    with pytest.raises(ConfigError, match=r"config\.toml.*guardrails"):
        load_config(tmp_path / "config.toml")


def test_r5_a_config_with_an_unknown_or_missing_key_is_a_config_error(tmp_path):
    (tmp_path / "config.toml").write_text(
        RULES_WITHOUT_GUARDRAILS + "\n[guardrails]\nacwr_min = 0.8\n")
    with pytest.raises(ConfigError, match="guardrails"):
        load_config(tmp_path / "config.toml")


def test_r5_an_athlete_file_the_coach_wrote_wrongly_is_a_config_error(tmp_path):
    (tmp_path / "athlete.toml").write_text("[baseline]\nfloor_km = 30.0\n")
    with pytest.raises(ConfigError, match=r"athlete\.toml"):
        load_config("tests/fixtures/config.toml", tmp_path / "athlete.toml")


def test_r5_text_that_is_not_toml_is_a_config_error(tmp_path):
    (tmp_path / "config.toml").write_text("[regime\ncold_max_days = 13\n")
    with pytest.raises(ConfigError, match=r"config\.toml"):
        load_config(tmp_path / "config.toml")


@pytest.fixture
def garmin_stub(monkeypatch):
    """A Garmin that answers with an empty history, so a command reaches its config."""
    monkeypatch.setattr(cli, "build_client", lambda *args: object())
    monkeypatch.setattr(cli, "fetch_snapshot",
                        lambda client, as_of, days: _snapshot(as_of))


def test_r5_the_cli_reports_a_broken_config_as_a_broken_config(
        tmp_path, monkeypatch, capsys, garmin_stub):
    (tmp_path / "config.toml").write_text(RULES_WITHOUT_GUARDRAILS)
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", str(tmp_path / "config.toml"))

    assert cli.main(["snapshot"]) == 1
    err = capsys.readouterr().err
    assert "guardrails" in err and "environment" not in err


def test_r5_a_missing_setting_is_reported_by_name():
    with pytest.raises(MissingSettingError, match="GARMIN_EMAIL"):
        garmin_reader.build_client()


def test_r5_an_unrelated_key_error_is_not_dressed_up_as_a_missing_setting(monkeypatch):
    """A bug must look like a bug: a wrong diagnosis sends the reader to .env."""
    def broken(args):
        raise KeyError("startTimeLocal")
    monkeypatch.setattr(cli, "_run", broken)
    with pytest.raises(KeyError):
        cli.main(["snapshot"])


# --- R6: Garmin failing after the login ----------------------------------------------

class RateLimitedClient:
    def get_activities_by_date(self, start, end):
        raise RuntimeError("429 Too Many Requests")


def test_r6_a_failure_while_reading_garmin_has_a_name():
    with pytest.raises(GarminReadError, match="429"):
        fetch_snapshot(RateLimitedClient(), TODAY, 28)


def test_r6_the_cli_stops_with_that_message_instead_of_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_client", lambda *args: RateLimitedClient())

    assert cli.main(["snapshot"]) == 1
    assert "429" in capsys.readouterr().err
