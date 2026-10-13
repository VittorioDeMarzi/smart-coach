"""Configuration loading. Every tunable rule lives in config/rules.toml; one athlete's
own numbers live in their data/athlete.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, TypeVar

# What belongs to one athlete and not to the rules: a separate file, so a copy of the
# project for someone else starts without it.
ATHLETE_SECTIONS = ("baseline", "history")
T = TypeVar("T")


@dataclass(frozen=True)
class RegimeConfig:
    cold_max_days: int
    warm_max_days: int
    decay_threshold_weeks: int


@dataclass(frozen=True)
class GuardrailConfig:
    acwr_min: float
    acwr_max: float
    acwr_block: float
    weekly_volume_increase_max_pct: float
    weekly_volume_increase_block_pct: float
    volume_reference_weeks: int
    long_run_increase_max_pct: float
    long_run_increase_block_pct: float
    long_run_reference_days: int
    long_run_share_max_pct: float
    min_easy_days_after_quality: int
    down_week_every: int
    fallback_weekly_km_cap: float
    fallback_long_run_km_cap: float


@dataclass(frozen=True)
class DataGap:
    """A period with no recordings during which the athlete kept training.

    Only the athlete knows this: from Garmin's data a broken watch and a three-month
    layoff look identical, and treating one as the other is wrong half the time
    (spec 8.5).
    """

    start: date
    end: date
    reason: str = ""


@dataclass(frozen=True)
class HistoryConfig:
    data_gaps: tuple[DataGap, ...] = ()


@dataclass(frozen=True)
class WorkoutConfig:
    easy_step_target: str


@dataclass(frozen=True)
class Baseline:
    floor_weekly_km: float = 0.0
    floor_long_run_km: float = 0.0


@dataclass(frozen=True)
class Config:
    regime: RegimeConfig
    guardrails: GuardrailConfig
    workout: WorkoutConfig
    baseline: Baseline
    history: HistoryConfig = HistoryConfig()


class ConfigError(ValueError):
    """A configuration file is missing, unreadable or does not have the expected keys.
    Carries the file and the section, so the reader knows where to look."""


class MissingSettingError(KeyError):
    """A setting expected in the environment (.env) is not there.

    Its own type because a bare KeyError can come from anywhere: reporting every one of
    them as a missing setting sends the reader to .env for what is a bug elsewhere.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"Missing environment variable: {self.name}"


def required_setting(name: str) -> str:
    try:
        return os.environ[name]
    except KeyError:
        raise MissingSettingError(name) from None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError as missing:
        raise ConfigError(f"{path}: file not found") from missing
    except tomllib.TOMLDecodeError as invalid:
        raise ConfigError(f"{path}: invalid TOML: {invalid}") from invalid


def _build(kind: Callable[..., T], values: Any, path: Path, section: str) -> T:
    """Build one config dataclass, turning a wrong or missing key into a ConfigError."""
    try:
        return kind(**values)
    except TypeError as wrong:
        raise ConfigError(f"{path}: section [{section}]: {wrong}") from wrong


def _section(raw: dict[str, Any], name: str, kind: Callable[..., T], path: Path) -> T:
    if name not in raw:
        raise ConfigError(f"{path}: missing section [{name}]")
    return _build(kind, raw[name], path, name)


def _load_athlete(path: Path | None) -> tuple[Baseline, HistoryConfig]:
    """The athlete's own numbers, kept apart from the rules so the rules can be handed
    to someone else.

    No file is a new athlete: no floor and no declared gap. That is the strictest
    reading, since a floor can only loosen a cap and a gap can only lengthen the history.
    """
    if path is None or not path.exists():
        return Baseline(), HistoryConfig()
    raw = _read_toml(path)
    gaps = raw.get("history", {}).get("data_gaps", ())
    return (_build(Baseline, raw.get("baseline", {}), path, "baseline"),
            HistoryConfig(data_gaps=tuple(
                _build(DataGap, gap, path, "history.data_gaps") for gap in gaps)))


def load_config(path: str | Path, athlete_path: str | Path | None = None) -> Config:
    """Read the rules from `path` and one athlete's numbers from `athlete_path`.

    The two are separate files in separate places: the rules travel with the code, the
    athlete's numbers stay on their machine. With no athlete file the athlete is a new
    one. Anything missing or misspelt raises ConfigError here, at load time.
    """
    rules_path = Path(path)
    raw = _read_toml(rules_path)
    misplaced = [section for section in ATHLETE_SECTIONS if section in raw]
    if misplaced:
        # Ignoring them would silently drop a declared gap and plan for a beginner.
        raise ConfigError(f"{rules_path}: {misplaced} belong in the athlete's own "
                          f"athlete.toml, not in the rules")
    baseline, history = _load_athlete(Path(athlete_path) if athlete_path else None)
    return Config(
        regime=_section(raw, "regime", RegimeConfig, rules_path),
        guardrails=_section(raw, "guardrails", GuardrailConfig, rules_path),
        workout=_section(raw, "workout", WorkoutConfig, rules_path),
        baseline=baseline,
        history=history,
    )
