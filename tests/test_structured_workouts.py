"""Structured sessions: plan.md carries intervals.icu workout-builder lines verbatim.

The builder's two hard rules drive these tests: `m` means minutes (metres are `mtr`),
and a repeat block needs one empty line before and after it.
"""

from datetime import date

import pytest

from smart_coach.config import load_config
from smart_coach.intervals_pusher import workout_description
from smart_coach.models import Step, Week, Workout
from smart_coach.plan_writer import PlanParseError, read_plan, write_plan

STRUCTURED = """# Plan

**Goal:** Half marathon, Florence, 2026-11-29

## 2026-W40

- 2026-09-28 | Easy | 8.25 km | easy
- 2026-09-30 | Fartlek | 10.0 km | quality
  - Warmup 10m Z2 Pace
  6x
    - 2m 4:10/km Pace
    - 90s Z2 Pace
  - Cooldown 10m Z2 Pace
"""


def _plan(tmp_path, text=STRUCTURED):
    path = tmp_path / "plan.md"
    path.write_text(text)
    return read_plan(path)


def test_step_lines_are_read_verbatim_with_their_nesting(tmp_path):
    fartlek = _plan(tmp_path).weeks[0].workouts[1]
    assert [(s.text, s.depth) for s in fartlek.steps] == [
        ("- Warmup 10m Z2 Pace", 0),
        ("6x", 0),
        ("- 2m 4:10/km Pace", 1),
        ("- 90s Z2 Pace", 1),
        ("- Cooldown 10m Z2 Pace", 0),
    ]


def test_a_workout_without_steps_keeps_no_steps(tmp_path):
    easy = _plan(tmp_path).weeks[0].workouts[0]
    assert easy.steps == ()
    assert easy.total_km == 8.25


def test_round_trip_preserves_steps_and_fractional_distances(tmp_path):
    original = _plan(tmp_path)
    out = tmp_path / "again.md"
    write_plan(original, out)
    assert read_plan(out) == original


def test_description_frames_the_repeat_block_with_blank_lines(tmp_path):
    """intervals.icu requires one empty line before and after every repeat block."""
    fartlek = _plan(tmp_path).weeks[0].workouts[1]
    assert workout_description(fartlek, "Z2 Pace") == (
        "- Warmup 10m Z2 Pace\n"
        "\n"
        "6x\n"
        "- 2m 4:10/km Pace\n"
        "- 90s Z2 Pace\n"
        "\n"
        "- Cooldown 10m Z2 Pace"
    )


def test_step_text_is_never_rewritten(tmp_path):
    """`m` is minutes and `mtr` is metres: reinterpreting a step would silently change
    the session. The text the coach wrote is the text intervals.icu receives."""
    plan = _plan(tmp_path, STRUCTURED.replace("- 2m 4:10/km Pace",
                                              "- 400mtr 3:50/km Pace"))
    assert "- 400mtr 3:50/km Pace" in workout_description(plan.weeks[0].workouts[1], "Z2 Pace")


def test_a_workout_without_steps_still_gets_a_target(tmp_path):
    """Every documented intervals.icu step carries a target, and a WORKOUT event whose
    description does not parse as steps is rejected. A bare "- 8.25km" is an unverified
    shape, so the fallback takes the target from config instead of omitting it."""
    easy = _plan(tmp_path).weeks[0].workouts[0]
    assert workout_description(easy, "Z2 Pace") == "- 8.25km Z2 Pace"


def test_the_easy_target_comes_from_config_not_from_the_code():
    """Which zone an easy run is run in is a training decision: it belongs in the rules,
    not hardcoded in the pusher."""
    assert load_config("tests/fixtures/config.toml").workout.easy_step_target


def test_an_indented_bullet_without_a_workout_is_rejected(tmp_path):
    """In this format an indented bullet is a step. Dropping one silently is the
    failure mode the review already caught once; prose must be unindented."""
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n  - Warmup 10m\n")
    with pytest.raises(PlanParseError) as caught:
        read_plan(path)
    assert caught.value.line_number == 7


def test_unindented_prose_under_a_week_is_still_ignored(tmp_path):
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n**Goal:** Test\n\n## 2026-W40\n\n"
                    "Down week: HRV below baseline for 5 days.\n\n"
                    "- 2026-09-28 | Easy | 6.0 km | easy\n")
    plan = read_plan(path)
    assert plan.weeks[0].total_km == 6.0


def test_steps_do_not_change_the_planned_volume(tmp_path):
    """Guardrails work on total_km from the workout line, never on the step text:
    a time-based step carries no distance to sum."""
    week = _plan(tmp_path).weeks[0]
    assert week.total_km == 18.25
    assert week.quality_days == (date(2026, 9, 30),)


def test_steps_are_immutable():
    step = Step(text="- 10m Z2 Pace", depth=0)
    with pytest.raises(Exception):
        step.text = "- 20m Z2 Pace"
    assert Week(iso_year=2026, iso_week=40, workouts=(
        Workout(day=date(2026, 9, 28), name="E", sport="running",
                steps=(step,), total_km=5.0, is_quality=False),)).total_km == 5.0
