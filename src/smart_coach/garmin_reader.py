"""Read Garmin Connect into a Snapshot.

Garmin's web API is unofficial and its payload shapes are undocumented and nested, so
every extraction is best-effort and yields None on any missing path. None means missing,
never zero: the guardrails treat the two differently on purpose (spec 8.3).

Distances arrive in metres and are converted to kilometres here, in one place.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Callable, Protocol

from smart_coach.config import required_setting
from smart_coach.load import AthleteProfile, acute_chronic_load
from smart_coach.models import Activity, Snapshot

METRES_PER_KM = 1000.0
# Garmin's own default location. Tokens land here after the first login and refresh
# themselves, so a multi-factor code is asked once rather than on every run.
TOKEN_STORE = "~/.garminconnect"
TOKEN_STORE_VARIABLE = "GARMIN_TOKEN_STORE"
# Load needs 28 days; how long the athlete has been recording needs much more, or the
# regime is capped by the window and reads WARM forever (spec 8.2).
HISTORY_DAYS = 180
# Wellness data for today is absent until the watch syncs, so every daily metric is
# looked up backwards from as_of until one has a value. These metrics move slowly:
# yesterday's resting heart rate is a good answer, silently dropping it is not.
DAILY_LOOKBACK_DAYS = 7
RUNNING_TYPE_KEYS = frozenset({"running", "trail_running", "treadmill_running",
                               "track_running", "indoor_running"})


class GarminError(RuntimeError):
    """Garmin Connect could not be used. A named type so the CLI can report it as a
    stop rather than letting whatever the unofficial library raises escape as a
    traceback (spec 11)."""


class GarminAuthError(GarminError):
    """Login failed: wrong credentials, or an MFA code is required."""


class GarminReadError(GarminError):
    """The login worked and reading the data did not: a rate limit, a timeout, or a
    payload whose shape changed. Nothing was read, so nothing may be decided on it."""


class GarminClient(Protocol):
    def get_activities_by_date(self, start: str, end: str) -> list[dict[str, Any]]: ...
    def get_training_status(self, day: str) -> Any: ...
    def get_max_metrics(self, day: str) -> Any: ...
    def get_rhr_day(self, day: str) -> Any: ...
    def get_hrv_data(self, day: str) -> Any: ...
    def get_user_profile(self) -> Any: ...
    def get_heart_rate_zones(self) -> Any: ...


def _dig(payload: Any, *keys: str | int) -> Any:
    """Follow a nested path, returning None instead of raising on any missing step."""
    current = payload
    for key in keys:
        if current is None:
            return None
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError):
            return None
    return current


def _latest_daily(
    fetch: Callable[[str], Any], as_of: date, *path: str | int
) -> Any:
    """Most recent value of a daily metric, walking back from `as_of`.

    Stops at the first day that has one: Garmin rate-limits by IP, so calls we do not
    need are calls we may not get back later.
    """
    for offset in range(DAILY_LOOKBACK_DAYS):
        value = _dig(fetch((as_of - timedelta(days=offset)).isoformat()), *path)
        if value is not None:
            return value
    return None


def _normalise_sport(type_key: str | None) -> str:
    if type_key in RUNNING_TYPE_KEYS:
        return "running"
    return type_key or "unknown"


def _to_activity(raw: dict[str, Any]) -> Activity:
    metres = raw.get("distance") or 0.0
    return Activity(
        day=date.fromisoformat(raw["startTimeLocal"][:10]),
        sport=_normalise_sport(_dig(raw, "activityType", "typeKey")),
        distance_km=round(metres / METRES_PER_KM, 2),
        duration_s=int(raw.get("duration") or 0),
        avg_hr=raw.get("averageHR"),
        location=raw.get("locationName"),
        elevation_gain_m=raw.get("elevationGain"),
    )


def _training_loads(payload: Any) -> tuple[float | None, float | None]:
    devices = _dig(payload, "mostRecentTrainingLoadBalance",
                   "metricsTrainingLoadBalanceDTOMap")
    if not isinstance(devices, dict):
        return None, None
    for entry in devices.values():
        chronic = _dig(entry, "chronicTrainingLoad")
        acute = _dig(entry, "acuteTrainingLoad")
        if chronic is not None or acute is not None:
            return chronic, acute
    return None, None


def _athlete_profile(client: GarminClient, resting_hr: int | None) -> AthleteProfile:
    """The constants TRIMP needs, read from Garmin rather than asked of the athlete.

    Max heart rate comes from the configured zones, which is where Garmin keeps it;
    anything missing yields an unusable profile and therefore no load at all.
    """
    max_hr = _dig(client.get_heart_rate_zones(), 0, "maxHeartRateUsed")
    gender = _dig(client.get_user_profile(), "userData", "gender")
    return AthleteProfile(resting_hr=resting_hr, max_hr=max_hr,
                          is_male=(gender != "FEMALE"))


def fetch_snapshot(
    client: GarminClient, as_of: date, days: int, history_days: int = HISTORY_DAYS
) -> Snapshot:
    """Snapshot ending at `as_of`.

    Activities are read over `history_days` so the regime can see the athlete's real
    history; volume and load are measured over the shorter `days` window. Any failure
    becomes GarminReadError: a snapshot is all or nothing.
    """
    try:
        return _read_snapshot(client, as_of, days, history_days)
    except Exception as failure:  # the unofficial client raises several unrelated types
        raise GarminReadError(
            f"Garmin read failed ({type(failure).__name__}): {failure}") from failure


def _read_snapshot(
    client: GarminClient, as_of: date, days: int, history_days: int
) -> Snapshot:
    start = (as_of - timedelta(days=history_days)).isoformat()
    end = as_of.isoformat()
    day = as_of.isoformat()

    activities = tuple(_to_activity(raw)
                       for raw in client.get_activities_by_date(start, end))
    resting = _latest_daily(client.get_rhr_day, as_of, "allMetrics", "metricsMap",
                            "WELLNESS_RESTING_HEART_RATE", 0, "value")

    chronic, acute = _training_loads(client.get_training_status(day))
    if chronic is None or acute is None:
        # Lifestyle watches do not report load; derive it from the activities instead.
        acute, chronic = acute_chronic_load(activities, as_of,
                                            _athlete_profile(client, resting))

    return Snapshot(
        as_of=as_of,
        activities=activities,
        chronic_load=chronic,
        acute_load=acute,
        # Observed 2026-09-26 against a real account: get_max_metrics returns a list
        # with one entry per user, not a dict.
        vo2max=_latest_daily(client.get_max_metrics, as_of, 0, "generic",
                             "vo2MaxPreciseValue"),
        resting_hr=resting,
        hrv_status=_latest_daily(client.get_hrv_data, as_of, "hrvSummary", "status"),
        window_days=days,
    )


def _garmin_login(
    email: str, password: str, prompt_mfa: Callable[[], str] | None, tokenstore: str
) -> Any:
    """The one place that talks to the library, so tests can replace it."""
    from garminconnect import Garmin

    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(tokenstore)
    return client


def build_client(prompt_mfa: Callable[[], str] | None = None) -> Any:
    """Authenticated Garmin client.

    `prompt_mfa` is called only when Garmin asks for a one-time code, and the code is
    the athlete's to supply — never this program's. It is asked once: `login` writes the
    tokens to TOKEN_STORE and reuses them afterwards.

    A missing credential raises MissingSettingError with the variable name; any login
    failure becomes GarminAuthError. Both are reported by the CLI, never as a traceback.
    """
    email = required_setting("GARMIN_EMAIL")
    password = required_setting("GARMIN_PASSWORD")
    # Cached tokens win over the credentials above, so two athletes sharing a store
    # means the second silently reads the first one's runs. One store per athlete.
    token_store = os.environ.get(TOKEN_STORE_VARIABLE) or TOKEN_STORE
    try:
        return _garmin_login(email, password, prompt_mfa, token_store)
    except GarminAuthError:
        raise
    except Exception as failure:  # the unofficial client raises several unrelated types
        raise GarminAuthError(f"Garmin login failed: {failure}") from failure
