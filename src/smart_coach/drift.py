"""Heart-rate drift (aerobic decoupling, Pa:HR) of a continuous run.

How much heart rate rose against speed from the first half of the run to the second.
Context for the coach, like climb and place: no guardrail reads it, so only `snapshot`
downloads the activity files it needs.

Only continuous runs are measured. A run with recovery or rest laps, or with more than
one workout step between warm-up and cool-down (intervals, strides, a fast finish, a
fartlek built as a workout), has no steady effort to compare, and its drift would be
noise. Warm-up and cool-down laps are left out of the runs that are measured.
"""

from __future__ import annotations

import io
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from garmin_fit_sdk import Decoder, Stream

from smart_coach.garmin_reader import GarminClient
from smart_coach.models import Activity, Sample

# Below an hour, what is left after the settling minutes is too short a steady effort
# for the drift to be more than noise.
MIN_DRIFT_DURATION_S = 3600
# Heart rate is still climbing to steady state during the first minutes.
HR_SETTLE_S = 600
# Slower than this is a stop (traffic lights), not running.
MIN_MOVING_SPEED_MPS = 1.5
# Below this share of usable samples the two halves are not comparable.
MIN_VALID_FRACTION = 0.8
FIT_SUFFIX = ".fit"
# Lap intensities as the FIT SDK decodes them. Free runs label every lap "interval";
# workouts label theirs warmup / active / recovery / cooldown.
EASING_INTENSITIES = frozenset({"warmup", "cooldown"})
BREAK_INTENSITIES = frozenset({"recovery", "rest"})


class FitFileError(ValueError):
    """The download is not a readable activity file."""


@dataclass(frozen=True)
class Lap:
    start_s: float
    end_s: float
    intensity: str | None
    # The workout step the lap belongs to; None on a run without a workout.
    step_index: int | None = None

    def contains(self, elapsed_s: float) -> bool:
        return self.start_s <= elapsed_s < self.end_s


@dataclass(frozen=True)
class FitRun:
    samples: tuple[Sample, ...]
    laps: tuple[Lap, ...]

    @property
    def is_continuous(self) -> bool:
        """No break, and one workout step between warm-up and cool-down at most.

        A second step is a change of pace by design (strides, a fast finish), and a
        drift across it measures the plan, not the athlete.
        """
        main = tuple(lap for lap in self.laps if lap.intensity not in EASING_INTENSITIES)
        steps = {lap.step_index for lap in main if lap.step_index is not None}
        return len(steps) <= 1 and not any(lap.intensity in BREAK_INTENSITIES
                                           for lap in main)


def read_fit(zip_bytes: bytes) -> FitRun:
    """The samples and laps of the single FIT file inside a Garmin download."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(FIT_SUFFIX)]
        if len(names) != 1:
            raise FitFileError(f"expected one {FIT_SUFFIX} file in the download, "
                               f"found {len(names)}")
        return _to_fit_run(_decode(archive.read(names[0])))


def _decode(fit: bytes) -> dict[str, Any]:
    messages, errors = Decoder(Stream.from_byte_array(bytearray(fit))).read()
    if errors:
        raise FitFileError(f"FIT decoding failed ({type(errors[0]).__name__})")
    return messages


def _to_fit_run(messages: dict[str, Any]) -> FitRun:
    records = [r for r in messages.get("record_mesgs", []) if r.get("timestamp")]
    if not records:
        return FitRun(samples=(), laps=())
    start: datetime = records[0]["timestamp"]
    samples = tuple(Sample(elapsed_s=(r["timestamp"] - start).total_seconds(),
                           speed_mps=_speed(r), heart_rate=r.get("heart_rate"))
                    for r in records)
    return FitRun(samples=samples, laps=tuple(_to_lap(raw, start)
                                              for raw in messages.get("lap_mesgs", [])))


def _speed(record: dict[str, Any]) -> float | None:
    speed = record.get("enhanced_speed")
    return record.get("speed") if speed is None else speed


def _to_lap(raw: dict[str, Any], start: datetime) -> Lap:
    if raw.get("start_time") is None or raw.get("total_elapsed_time") is None:
        raise FitFileError("a lap has no start time or no duration")
    lap_start = (raw["start_time"] - start).total_seconds()
    return Lap(start_s=lap_start, end_s=lap_start + raw["total_elapsed_time"],
               intensity=raw.get("intensity"), step_index=raw.get("wkt_step_index"))


def steady_samples(run: FitRun) -> tuple[Sample, ...]:
    """The samples outside warm-up and cool-down laps, which would fake a drift.

    A file whose laps carry no intensity keeps every sample.
    """
    easing = tuple(lap for lap in run.laps if lap.intensity in EASING_INTENSITIES)
    return tuple(s for s in run.samples
                 if not any(lap.contains(s.elapsed_s) for lap in easing))


def decoupling(samples: tuple[Sample, ...]) -> float | None:
    """Percent loss of speed per heartbeat from the first half to the second.

    None when too little of the run is usable: missing data is never a zero drift.
    """
    settled = tuple(s for s in samples if s.elapsed_s >= HR_SETTLE_S)
    usable = tuple(s for s in settled if s.heart_rate and s.speed_mps is not None
                   and s.speed_mps >= MIN_MOVING_SPEED_MPS)
    if not usable or len(usable) < MIN_VALID_FRACTION * len(settled):
        return None
    middle = (usable[0].elapsed_s + usable[-1].elapsed_s) / 2
    first = tuple(s for s in usable if s.elapsed_s < middle)
    second = tuple(s for s in usable if s.elapsed_s >= middle)
    if not first or not second:
        return None
    first_ef, second_ef = _efficiency(first), _efficiency(second)
    return (first_ef - second_ef) / first_ef * 100


def _efficiency(samples: tuple[Sample, ...]) -> float:
    """Mean speed over mean heart rate."""
    speed = sum(s.speed_mps for s in samples) / len(samples)
    heart_rate = sum(s.heart_rate for s in samples) / len(samples)
    return speed / heart_rate


def run_drifts(client: GarminClient, runs: tuple[Activity, ...]) -> dict[int, float | None]:
    """Drift of every continuous run long enough to have one, keyed by activity id.

    A run left out was not measured; None means it should have been and could not be.
    One unreadable file must not hide the rest of the snapshot, since drift feeds no
    decision: its failure is reported on stderr, without the body, and becomes None.
    """
    drifts: dict[int, float | None] = {}
    for run in runs:
        if run.activity_id is None or run.duration_s < MIN_DRIFT_DURATION_S:
            continue
        try:
            fit_run = read_fit(client.activity_fit(run.activity_id))
        except (requests.RequestException, zipfile.BadZipFile, FitFileError) as failure:
            print(f"drift of the {run.day} run not read ({type(failure).__name__})",
                  file=sys.stderr)
            drifts[run.activity_id] = None
            continue
        if fit_run.is_continuous:
            drifts[run.activity_id] = decoupling(steady_samples(fit_run))
    return drifts
