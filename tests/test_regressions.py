"""Regression tests from the final review. Each test name carries its finding id.

Every one of these reproduced a real defect before the fix pass: they exist so the
defect cannot come back silently.
"""

from dataclasses import replace
from datetime import date, timedelta

import pytest
import responses

from smart_coach.config import Baseline, load_config
from smart_coach.guardrails import blocking, validate
from smart_coach.intervals_pusher import push_week
from smart_coach.models import Activity, Regime, Snapshot, Step, Week, Workout
from smart_coach.plan_writer import PlanParseError, read_plan
from smart_coach.regime import determine_regime

CONFIG = load_config("tests/fixtures/config.toml")

# ISO week 40 of 2026 is Mon 2026-09-28 .. Sun 2026-10-04; week 39 is the one before.
W40_DAYS = (date(2026, 9, 28), date(2026, 9, 30), date(2026, 10, 2), date(2026, 10, 4))
W39_DAYS = (date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 25), date(2026, 9, 27))


def _week(total_km: float, longest_km: float, *, days=W40_DAYS, iso_week: int = 40) -> Week:
    each = (total_km - longest_km) / (len(days) - 1)
    return Week(iso_year=2026, iso_week=iso_week, workouts=tuple(
        Workout(day=day, name=name, sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=km, is_quality=False)
        for day, name, km in zip(days, ("Long", "E1", "E2", "E3"),
                                 (longest_km, each, each, each))
    ))


def _snapshot(chronic, acute, activities=()) -> Snapshot:
    return Snapshot(as_of=date(2026, 9, 27), activities=activities, chronic_load=chronic,
                    acute_load=acute, vo2max=None, resting_hr=None, hrv_status=None)


def _run(day: date, km: float) -> Activity:
    return Activity(day=day, sport="running", distance_km=km, duration_s=1800, avg_hr=140)


def _rules(violations) -> set[str]:
    return {v.rule for v in violations}


# --- C1: zero is not "present" -------------------------------------------------------

def test_c1_zero_chronic_load_does_not_disable_both_the_rule_and_its_fallback():
    """A zero load is an unworn device, not a rested athlete. Skipping the rule AND its
    replacement is the skip_check() bug the spec forbids."""
    week = _week(90.0, 30.0)
    violations = validate(week, None, _snapshot(0.0, 500.0), Regime.COLD, CONFIG)
    assert violations, "a 90 km week with a 30 km long run must never be admissible"
    assert "absolute_volume_cap" in _rules(violations)


def test_c1_zero_chronic_load_in_hot_regime_still_falls_back():
    violations = validate(_week(90.0, 30.0), None, _snapshot(0.0, 500.0), Regime.HOT, CONFIG)
    assert "absolute_volume_cap" in _rules(violations)


# --- C2: the regime decides whether the load is trustworthy --------------------------

def test_c2_warm_regime_replaces_the_load_rules_with_absolute_caps():
    """Under 28 days Garmin still reports a number, but it is built from too short a
    window to mean anything (spec 8.1), so the caps must replace the ratio (spec 8.2)."""
    violations = validate(_week(90.0, 30.0), None, _snapshot(300.0, 300.0),
                          Regime.WARM, CONFIG)
    assert "absolute_volume_cap" in _rules(violations)


def test_c2_cold_regime_replaces_the_load_rules_with_absolute_caps():
    violations = validate(_week(90.0, 30.0), None, _snapshot(300.0, 300.0),
                          Regime.COLD, CONFIG)
    assert "absolute_volume_cap" in _rules(violations)


def test_c2_hot_regime_with_trustworthy_load_uses_the_nominal_rules():
    """The caps must not fire when the ratio rule is genuinely applicable."""
    week = _week(40.0, 12.0)
    violations = validate(week, _week(38.0, 11.0, days=W39_DAYS, iso_week=39),
                          _snapshot(400.0, 420.0, (_run(date(2026, 9, 23), 38.0),)),
                          Regime.HOT, CONFIG)
    assert "absolute_volume_cap" not in _rules(violations)


# --- C3: the first week of a plan has no predecessor to compare against -------------

def test_c3_first_week_is_bounded_by_what_was_actually_run():
    """The week this system publishes first is the one with previous_week=None."""
    ran_100_km = tuple(_run(date(2026, 9, 1) + timedelta(days=n * 2), 10.0)
                       for n in range(10))
    violations = validate(_week(60.0, 18.0), None, _snapshot(300.0, 300.0, ran_100_km),
                          Regime.HOT, CONFIG)
    assert "weekly_volume_increase" in _rules(blocking(violations)), (
        "60 km proposed against a 25 km/week baseline must be refused")


def test_c3_a_first_week_close_to_the_actual_baseline_is_allowed():
    ran_100_km = tuple(_run(date(2026, 9, 1) + timedelta(days=n * 2), 10.0)
                       for n in range(10))
    violations = validate(_week(26.0, 8.0), None, _snapshot(300.0, 300.0, ran_100_km),
                          Regime.HOT, CONFIG)
    assert "weekly_volume_increase" not in _rules(violations)


# --- I2: walks are not running history ----------------------------------------------

def test_i2_auto_detected_walks_do_not_make_a_beginner_look_experienced():
    walks = tuple(Activity(day=date(2026, 9, 27) - timedelta(days=n),
                           sport="walking", distance_km=4.0, duration_s=2400, avg_hr=95)
                  for n in range(28))
    runs = (_run(date(2026, 9, 24), 6.0), _run(date(2026, 9, 26), 6.0))
    snapshot = _snapshot(200.0, 210.0, walks + runs)
    assert snapshot.days_of_data == 3
    assert determine_regime(snapshot, CONFIG) is Regime.COLD


def test_i2_days_of_data_is_measured_from_as_of_not_from_the_last_run():
    """A rest day on the window boundary must not shrink the history."""
    runs = (_run(date(2026, 8, 20), 8.0), _run(date(2026, 9, 20), 8.0))
    assert _snapshot(None, None, runs).days_of_data == 38


# --- I4 / I5: weeks the guardrails used to wave through or always refuse ------------

def test_i4_an_empty_week_is_never_admissible():
    empty = Week(iso_year=2026, iso_week=41, workouts=())
    assert "empty_week" in _rules(
        validate(empty, _week(40.0, 12.0), _snapshot(400.0, 420.0), Regime.HOT, CONFIG))


def test_i5_a_single_workout_week_is_not_refused_for_the_long_run_share():
    """A race week or a return-to-running week has one run: 100% is not a real share."""
    one_run = Week(iso_year=2026, iso_week=40, workouts=(
        Workout(day=date(2026, 9, 28), name="Race", sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=8.0, is_quality=False),))
    assert "long_run_share" not in _rules(
        validate(one_run, None, _snapshot(400.0, 420.0, (_run(date(2026, 9, 20), 30.0),)),
                 Regime.HOT, CONFIG))


# --- I7: the increase rule must not compare the plan against itself -----------------

def test_i7_volume_increase_is_measured_against_what_was_actually_run():
    """Planned 40, ran 20, next week planned 43: a +115% jump that used to read +7.5%."""
    actual_w39 = (_run(date(2026, 9, 22), 10.0), _run(date(2026, 9, 24), 10.0))
    violations = validate(_week(43.0, 13.0), _week(40.0, 12.0, days=W39_DAYS, iso_week=39),
                          _snapshot(400.0, 420.0, actual_w39), Regime.HOT, CONFIG)
    assert "weekly_volume_increase" in _rules(violations)


# --- I8: a floor built from history must not loosen a cap the decay rule imposed ----

def test_i8_the_baseline_floor_does_not_loosen_the_cap_in_cold_regime():
    generous = replace(CONFIG, baseline=Baseline(floor_weekly_km=70.0,
                                                 floor_long_run_km=30.0))
    violations = validate(_week(60.0, 18.0), None, _snapshot(None, None), Regime.COLD,
                          generous)
    assert "absolute_volume_cap" in _rules(violations), (
        "a return from a layoff must not inherit the pre-layoff volume as its cap")


# --- I1: a hand-edited plan.md must never be parsed partially in silence ------------

def test_i1_an_annotated_week_heading_is_rejected_not_merged(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("""# Plan

**Goal:** Test

## 2026-W40   -> build week

- 2026-09-28 | Easy | 10.0 km | easy
""")
    with pytest.raises(PlanParseError) as caught:
        read_plan(path)
    assert caught.value.line_number == 5


def test_i1_unrecognised_bullet_styles_are_rejected_not_dropped(tmp_path):
    for body in ("  - 2026-09-28 | Easy | 10.0 km | easy",
                 "* 2026-09-30 | Fartlek | 10.0 km | quality",
                 "-2026-10-02 | Tempo | 12.0 km | quality"):
        path = tmp_path / "plan.md"
        path.write_text(f"# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n{body}\n")
        plan = read_plan(path)
        assert plan.weeks[0].total_km > 0, f"silently dropped: {body!r}"


# --- I6: idempotency must count multiplicity, not membership ------------------------

ATHLETE = "i12345"
BASE = f"https://intervals.icu/api/v1/athlete/{ATHLETE}/events"
PUSH_DAY = date(2026, 9, 27)  # the Sunday before W40: the whole week is still ahead


def _double_day_week() -> Week:
    return Week(iso_year=2026, iso_week=40, workouts=tuple(
        Workout(day=date(2026, 9, 28), name="Double", sport="running",
                steps=(Step(text="- 5km Z2 Pace"),),
                total_km=6.0, is_quality=False)
        for _ in range(2)
    ))


@responses.activate
def test_i6_a_second_identical_session_is_still_published():
    """Two sessions on one day with the same name: one already there, one still missing."""
    responses.add(responses.GET, BASE, status=200, json=[
        {"id": 8, "external_id": "smart-coach:2026-09-28:0", "name": "Double",
         "start_date_local": "2026-09-28T00:00:00", "description": "- 5km Z2 Pace"}])
    responses.add(responses.POST, BASE, json={"id": 9}, status=200)

    result = push_week(_double_day_week(), ATHLETE, "key", "Z2 Pace", today=PUSH_DAY)

    assert (result.created, result.skipped) == (1, 1)


@responses.activate
def test_i6_created_count_survives_a_mid_push_failure():
    responses.add(responses.GET, BASE, json=[], status=200)
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)
    responses.add(responses.POST, BASE, status=500)

    week = Week(iso_year=2026, iso_week=40, workouts=(
        _week(20.0, 8.0).workouts[0], _week(20.0, 8.0).workouts[1]))
    result = push_week(week, ATHLETE, "key", "Z2 Pace", today=PUSH_DAY)

    assert result.error is not None
    assert result.created == 1, "the event that did land must be reported"


@responses.activate
def test_i6_an_event_without_a_name_does_not_escape_as_a_traceback():
    responses.add(responses.GET, BASE,
                  json=[{"start_date_local": "2026-09-28T00:00:00"}], status=200)
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)

    result = push_week(_week(20.0, 8.0), ATHLETE, "key", "Z2 Pace", today=PUSH_DAY)

    assert result.error is None
    assert result.created == 4


# --- C4 / I3: week selection and error surfacing in the CLI -------------------------

def _plan_file(tmp_path, headings_and_km) -> str:
    body = "# Plan\n\n**Goal:** Test\n\n"
    for (year, week), km in headings_and_km:
        monday = date.fromisocalendar(year, week, 1)
        body += f"## {year}-W{week:02d}\n\n- {monday} | Easy | {km:.1f} km | easy\n\n"
    path = tmp_path / "plan.md"
    path.write_text(body)
    return str(path)


def test_c4_no_week_flag_selects_next_week_not_the_oldest_in_the_file(tmp_path):
    """`push` with no flag used to republish week 1 of the cycle, months later."""
    from smart_coach.cli import select_week_index

    plan = read_plan(_plan_file(tmp_path, [((2026, 52), 10.0), ((2027, 1), 12.0),
                                           ((2027, 2), 14.0)]))
    # 2026-12-30 sits in ISO 2026-W53, so the week after it is 2027-W01: index 1, not 0.
    assert select_week_index(plan, None, None, today=date(2026, 12, 30)) == 1


def test_c4_week_selection_discriminates_on_the_iso_year(tmp_path):
    from smart_coach.cli import select_week_index

    plan = read_plan(_plan_file(tmp_path, [((2026, 52), 10.0), ((2027, 52), 20.0)]))
    assert select_week_index(plan, 52, 2027, today=date(2027, 1, 4)) == 1


def test_c4_a_missing_target_week_refuses_instead_of_guessing(tmp_path):
    from smart_coach.cli import select_week_index

    plan = read_plan(_plan_file(tmp_path, [((2027, 1), 12.0)]))
    with pytest.raises(SystemExit):
        select_week_index(plan, None, None, today=date(2026, 12, 21))


def test_i3_a_malformed_plan_reports_the_line_without_a_traceback(monkeypatch, capsys,
                                                                 tmp_path):
    from smart_coach import cli

    bad = tmp_path / "plan.md"
    bad.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n"
                   "- 2026-09-28 | Easy | otto km | easy\n")
    monkeypatch.setattr(cli, "DEFAULT_PLAN", str(bad))

    code = cli.main(["validate", "--week", "40"])

    assert code == 1
    assert "line 7" in capsys.readouterr().err


def test_i3_a_missing_environment_variable_is_reported_not_raised(monkeypatch, capsys,
                                                                 tmp_path):
    from smart_coach import cli

    monkeypatch.setattr(cli, "DEFAULT_PLAN",
                        _plan_file(tmp_path, [((2026, 40), 10.0)]))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)

    code = cli.main(["validate", "--week", "40"])

    assert code == 1
    assert "GARMIN_EMAIL" in capsys.readouterr().err
