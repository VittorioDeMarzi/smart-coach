"""Two severities: a violation either stops the week or flags it.

One gate for everything means a sensible week refused over two decimals, which teaches
the operator to bypass the check — and a guardrail bypassed by routine is worse than no
guardrail. Warnings exist ONLY between the nominal threshold and the hard one: nothing
that was unconditionally unsafe becomes advisory.
"""

from datetime import date

import pytest

from smart_coach.config import load_config
from smart_coach.guardrails import blocking, validate
from smart_coach.models import Regime, Severity, Snapshot, Step, Week, Workout

CONFIG = load_config("tests/fixtures/config.toml")
DAYS = (date(2026, 9, 28), date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 4))


def _week(total_km: float, longest_km: float | None = None) -> Week:
    longest = longest_km if longest_km is not None else total_km * 0.3
    each = (total_km - longest) / 3
    return Week(iso_year=2026, iso_week=40, workouts=tuple(
        Workout(day=day, name=name, sport="running",
                steps=(Step(text=f"- {km:g}km Z2 Pace"),), total_km=km, is_quality=False)
        for day, name, km in zip(DAYS, ("Long", "E1", "E2", "E3"),
                                 (longest, each, each, each))))


def _snapshot(chronic, acute) -> Snapshot:
    return Snapshot(as_of=date(2026, 9, 27), activities=(), chronic_load=chronic,
                    acute_load=acute, vo2max=None, resting_hr=None, hrv_status=None)


def _by_rule(violations) -> dict:
    return {v.rule: v.severity for v in violations}


def test_a_ratio_just_over_the_band_warns_rather_than_blocking():
    """A ratio of 1.46 is what a good fortnight after a quiet one looks like. Refusing
    the whole week for it is how a guardrail gets ignored."""
    found = _by_rule(validate(_week(40.0), None, _snapshot(400.0, 584.0), Regime.HOT,
                              CONFIG))
    assert found["acwr_band"] is Severity.WARN


def test_a_ratio_far_over_the_band_blocks():
    found = _by_rule(validate(_week(40.0), None, _snapshot(400.0, 760.0), Regime.HOT,
                              CONFIG))
    assert found["acwr_band"] is Severity.BLOCK


def test_a_ratio_below_the_band_only_ever_warns():
    """Training less than the chronic average is never dangerous."""
    found = _by_rule(validate(_week(40.0), None, _snapshot(400.0, 200.0), Regime.HOT,
                              CONFIG))
    assert found["acwr_band"] is Severity.WARN


def test_a_modest_volume_rise_warns_and_a_steep_one_blocks():
    previous = _week(40.0)
    modest = _by_rule(validate(_week(46.0), previous, _snapshot(400.0, 420.0),
                               Regime.HOT, CONFIG))
    steep = _by_rule(validate(_week(56.0), previous, _snapshot(400.0, 420.0),
                              Regime.HOT, CONFIG))
    assert modest["weekly_volume_increase"] is Severity.WARN
    assert steep["weekly_volume_increase"] is Severity.BLOCK


def test_the_long_run_share_only_ever_warns():
    """How long the long run may be is graded by the spike rule, which has evidence
    behind it. The share is a rule of thumb: it flags the lopsided week and never
    refuses it, because the most lopsided week of a plan is the race week."""
    ordinary = _by_rule(validate(_week(40.0, longest_km=22.0), None,
                                 _snapshot(400.0, 420.0), Regime.HOT, CONFIG))
    lopsided = _by_rule(validate(_week(40.0, longest_km=38.0), None,
                                 _snapshot(400.0, 420.0), Regime.HOT, CONFIG))
    assert "long_run_share" not in ordinary
    assert lopsided["long_run_share"] is Severity.WARN


def test_the_fail_closed_fallback_is_never_advisory():
    """When the load cannot be computed the system is blind. Blind and permissive is the
    combination this whole design exists to prevent."""
    found = _by_rule(validate(_week(90.0, 30.0), None, _snapshot(None, None),
                              Regime.COLD, CONFIG))
    assert found["absolute_volume_cap"] is Severity.BLOCK


def test_structural_rules_block_because_they_are_not_a_matter_of_degree():
    quality_back_to_back = Week(iso_year=2026, iso_week=40, workouts=(
        Workout(day=date(2026, 9, 29), name="Q", sport="running",
                steps=(Step(text="- 10km Z4 Pace"),), total_km=10.0, is_quality=True),
        Workout(day=date(2026, 9, 30), name="Q", sport="running",
                steps=(Step(text="- 10km Z4 Pace"),), total_km=10.0, is_quality=True)))
    found = _by_rule(validate(quality_back_to_back, None, _snapshot(400.0, 420.0),
                              Regime.HOT, CONFIG))
    assert found["no_back_to_back_quality"] is Severity.BLOCK

    empty = _by_rule(validate(Week(iso_year=2026, iso_week=41, workouts=()), None,
                              _snapshot(400.0, 420.0), Regime.HOT, CONFIG))
    assert empty["empty_week"] is Severity.BLOCK


def test_blocking_selects_only_what_must_stop_the_week():
    violations = validate(_week(46.0), _week(40.0), _snapshot(400.0, 584.0),
                          Regime.HOT, CONFIG)
    assert violations, "this week does breach the nominal thresholds"
    assert blocking(violations) == (), "but nothing here is dangerous enough to refuse"


# --- what the CLI does with each kind ------------------------------------------------

CONFIG_PATH = "tests/fixtures/config.toml"


def test_a_week_with_only_warnings_passes_and_still_shows_them(capsys):
    from smart_coach.cli import validate_command

    code = validate_command(_week(46.0), _week(40.0), _snapshot(400.0, 584.0),
                            Regime.HOT, config_path=CONFIG_PATH)
    out = capsys.readouterr().out

    assert code == 0, "warnings must not refuse an otherwise sound week"
    assert "acwr_band" in out, "and they must still be visible: silent is not the point"
    assert "OK" in out


def test_a_week_with_a_block_fails(capsys):
    from smart_coach.cli import validate_command

    code = validate_command(_week(90.0, 30.0), None, _snapshot(None, None),
                            Regime.COLD, config_path=CONFIG_PATH)
    assert code == 1
    assert "absolute_volume_cap" in capsys.readouterr().out


def test_push_proceeds_on_warnings_and_refuses_on_blocks(monkeypatch, capsys):
    from smart_coach import cli

    pushed = []
    monkeypatch.setattr(cli, "push_week",
                        lambda *a, **k: pushed.append(a) or cli.PushResult(1, 0, None))

    warned = cli.push_command(_week(46.0), _week(40.0), _snapshot(400.0, 584.0),
                              Regime.HOT, athlete_id="i1", api_key="k",
                              config_path=CONFIG_PATH)
    assert warned == 0 and len(pushed) == 1

    blocked = cli.push_command(_week(90.0, 30.0), None, _snapshot(None, None),
                               Regime.COLD, athlete_id="i1", api_key="k",
                               config_path=CONFIG_PATH)
    assert blocked == 1 and len(pushed) == 1, "a block must not reach intervals.icu"
