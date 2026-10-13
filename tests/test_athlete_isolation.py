"""Nothing about one athlete may leak into another's plan.

The rules are generic; three things are personal: the declared data gaps and baseline
floors, the Garmin session, and the files the coach writes. Each has to be separable,
because a plan built on someone else's history looks perfectly plausible.
"""

from datetime import date

import pytest

from smart_coach import garmin_reader
from smart_coach.config import DataGap, load_config
from smart_coach.plan_writer import PlanNotFoundError, read_plan

RULES = """
[regime]
cold_max_days = 13
warm_max_days = 27
decay_threshold_weeks = 3

[guardrails]
acwr_min = 0.8
acwr_max = 1.3
acwr_block = 1.7
weekly_volume_increase_max_pct = 10.0
weekly_volume_increase_block_pct = 25.0
volume_reference_weeks = 4
long_run_increase_max_pct = 10.0
long_run_increase_block_pct = 30.0
long_run_reference_days = 30
long_run_share_max_pct = 60.0
min_easy_days_after_quality = 1
down_week_every = 4
fallback_weekly_km_cap = 45.0
fallback_long_run_km_cap = 16.0

[workout]
easy_step_target = "Z2 Pace"
"""

ATHLETE = """
[baseline]
floor_weekly_km = 30.0
floor_long_run_km = 12.0

[[history.data_gaps]]
start = 2026-06-12
end = 2026-08-31
reason = "watch broken"
"""


# --- the athlete's numbers live apart from the rules ---------------------------------

def test_the_athletes_numbers_are_read_from_their_own_file(tmp_path):
    """Rules and athlete live in different folders: one travels with the code, the
    other never leaves the machine."""
    (tmp_path / "rules.toml").write_text(RULES)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "athlete.toml").write_text(ATHLETE)
    config = load_config(tmp_path / "rules.toml", tmp_path / "elsewhere" / "athlete.toml")
    assert config.baseline.floor_weekly_km == 30.0
    assert config.history.data_gaps == (
        DataGap(start=date(2026, 6, 12), end=date(2026, 8, 31), reason="watch broken"),)


def test_an_athlete_file_next_to_the_rules_is_not_picked_up_by_accident(tmp_path):
    """Only the path that was asked for is read."""
    (tmp_path / "rules.toml").write_text(RULES)
    (tmp_path / "athlete.toml").write_text(ATHLETE)
    assert load_config(tmp_path / "rules.toml").history.data_gaps == ()


@pytest.mark.parametrize("athlete_file", [None, "absent.toml"])
def test_a_new_athlete_has_no_floor_and_no_declared_gap(tmp_path, athlete_file):
    """No athlete file is the strictest reading, never an error: a floor can only
    loosen a cap and a declared gap can only lengthen the history."""
    (tmp_path / "rules.toml").write_text(RULES)
    athlete_path = tmp_path / athlete_file if athlete_file else None
    config = load_config(tmp_path / "rules.toml", athlete_path)
    assert (config.baseline.floor_weekly_km, config.baseline.floor_long_run_km) == (0, 0)
    assert config.history.data_gaps == ()


def test_the_cli_reads_the_athletes_file_from_the_data_folder(tmp_path, monkeypatch):
    from smart_coach import cli

    (tmp_path / "athlete.toml").write_text(ATHLETE)
    monkeypatch.setattr(cli, "DEFAULT_ATHLETE", str(tmp_path / "athlete.toml"))
    assert cli._config("tests/fixtures/config.toml").baseline.floor_weekly_km == 30.0


def test_athlete_numbers_left_in_the_rules_file_are_refused_not_ignored(tmp_path):
    """Silently dropping a declared gap would turn an athlete into a beginner without
    a word."""
    (tmp_path / "rules.toml").write_text(RULES + ATHLETE)
    with pytest.raises(ValueError, match="athlete.toml"):
        load_config(tmp_path / "rules.toml")


# --- the Garmin session ---------------------------------------------------------------

@pytest.fixture
def login_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(garmin_reader, "_garmin_login",
                        lambda email, password, prompt, store: calls.append(store))
    monkeypatch.setenv("GARMIN_EMAIL", "friend@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "not-a-real-password")
    return calls


def test_the_token_store_can_be_set_per_project(login_calls, monkeypatch):
    """Cached tokens win over the credentials in .env. Two athletes sharing one store
    means the second reads the first one's runs without any error."""
    monkeypatch.setenv("GARMIN_TOKEN_STORE", "~/.config/smart-coach/friend-tokens")
    garmin_reader.build_client()
    assert login_calls == ["~/.config/smart-coach/friend-tokens"]


@pytest.mark.parametrize("unset", [None, ""])
def test_without_the_setting_the_default_store_is_used(login_calls, monkeypatch, unset):
    if unset is not None:
        monkeypatch.setenv("GARMIN_TOKEN_STORE", unset)
    garmin_reader.build_client()
    assert login_calls == [garmin_reader.TOKEN_STORE]


# --- a fresh clone has no athlete files at all ---------------------------------------

def test_the_example_athlete_file_declares_nothing(tmp_path):
    """Copied as it is, it must not invent a gap or a floor for the new athlete."""
    (tmp_path / "rules.toml").write_text(RULES)
    config = load_config(tmp_path / "rules.toml", "config/athlete.example.toml")
    assert config.history.data_gaps == ()
    assert config.baseline.floor_weekly_km == 0.0


def test_the_rules_shipped_with_the_project_load():
    """The file the commands really use, as opposed to the fixture the suite runs on."""
    assert load_config("config/rules.toml").guardrails.down_week_every > 0


def test_a_missing_plan_says_how_to_create_one(tmp_path):
    with pytest.raises(PlanNotFoundError, match="/coach"):
        read_plan(tmp_path / "plan.md")


def test_a_command_run_before_any_plan_exists_stops_with_that_message(
        tmp_path, monkeypatch, capsys):
    """The athlete's files are not in the repository, so the first command on a fresh
    clone finds none. That is the normal first run, not a crash."""
    from smart_coach import cli

    monkeypatch.setattr(cli, "DEFAULT_PLAN", str(tmp_path / "plan.md"))
    assert cli.main(["export"]) == 1
    assert "/coach" in capsys.readouterr().err
