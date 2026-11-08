"""Read Garmin Connect into a Snapshot, through Garmin's Personal API.

The Personal API is Garmin's official, read-only access to one's own data: a key created
on Garmin Connect Web is exchanged for a bearer token valid 24 hours. The payloads are
documented, but every extraction stays best-effort and yields None on any missing path.
None means missing, never zero: the guardrails treat the two differently on purpose
(spec 8.3).

Distances arrive in metres and are converted to kilometres here, in one place.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Protocol

import requests

from smart_coach.config import required_setting
from smart_coach.load import AthleteProfile, acute_chronic_load
from smart_coach.models import HISTORY_DAYS, Activity, Snapshot

METRES_PER_KM = 1000.0
BASE_URL = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
# A ZIP holding the activity's original FIT file.
ACTIVITY_FILE_PATH = "/download-service/files/activity/{activity_id}"
API_KEY_VARIABLE = "GARMIN_API_KEY"
REQUEST_TIMEOUT_S = 30
# The API's own default page size; a page shorter than this is the last one.
ACTIVITY_PAGE_SIZE = 99
REFUSED_KEY_STATUSES = frozenset({401, 403})
# Wellness data for today is absent until the watch syncs, so every daily metric is
# looked up backwards from as_of until one has a value. These metrics move slowly:
# yesterday's resting heart rate is a good answer, silently dropping it is not.
DAILY_LOOKBACK_DAYS = 7
RUNNING_TYPE_KEYS = frozenset({"running", "trail_running", "treadmill_running",
                               "track_running", "indoor_running"})
# Garmin keeps a max heart rate per sport; the running one, when the athlete set it, is
# the one their runs were measured against.
ZONE_SPORTS_BY_PREFERENCE = ("RUNNING", "DEFAULT")


class GarminError(RuntimeError):
    """Garmin Connect could not be used. A named type so the CLI can report it as a
    stop rather than letting an HTTP library error escape as a traceback (spec 11)."""


class GarminAuthError(GarminError):
    """No token: the key was refused, or Garmin could not be reached to exchange it."""


class GarminReadError(GarminError):
    """The token worked and reading the data did not: a rate limit, a timeout, or a
    payload whose shape changed. Nothing was read, so nothing may be decided on it."""


class GarminClient(Protocol):
    def activities(self, start: str, end: str) -> list[dict[str, Any]]: ...
    def daily_heart_rate(self, day: str) -> Any: ...
    def hrv(self, day: str) -> Any: ...
    def heart_rate_zones(self) -> Any: ...
    def activity_fit(self, activity_id: int) -> bytes: ...


class PersonalApiClient:
    """The Personal API reads a snapshot needs, over an authenticated session."""

    def __init__(self, session: requests.Session) -> None:
        self._session = session

    def _get(self, path: str, **params: Any) -> Any:
        """Decoded JSON, or None for a day Garmin has nothing for (204, empty body)."""
        response = self._session.get(BASE_URL + path, params=params,
                                     timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        return response.json() if response.text.strip() else None

    def _get_bytes(self, path: str) -> bytes:
        """The raw body of a file download."""
        response = self._session.get(BASE_URL + path, headers={"Accept": "*/*"},
                                     timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        return response.content

    def activities(self, start: str, end: str) -> list[dict[str, Any]]:
        """Every activity started from `start` to `end`, both included, page by page."""
        found: list[dict[str, Any]] = []
        while True:
            page = self._get(ACTIVITIES_PATH, startDate=start, endDate=end,
                             start=len(found), limit=ACTIVITY_PAGE_SIZE) or []
            found += page
            if len(page) < ACTIVITY_PAGE_SIZE:
                return found

    def daily_heart_rate(self, day: str) -> Any:
        return self._get("/wellness-service/wellness/dailyHeartRate", date=day)

    def hrv(self, day: str) -> Any:
        return self._get(f"/hrv-service/hrv/{day}")

    def heart_rate_zones(self) -> Any:
        return self._get("/biometric-service/heartRateZones/v2")

    def activity_fit(self, activity_id: int) -> bytes:
        """The activity's download: a ZIP holding its FIT file."""
        return self._get_bytes(ACTIVITY_FILE_PATH.format(activity_id=activity_id))


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

    Stops at the first day that has one: calls we do not need are calls a rate limit may
    not give back later.
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
        activity_id=raw.get("activityId"),
    )


def _max_heart_rate(zones: Any) -> int | None:
    """Max heart rate from the zones of the most specific sport that has one."""
    if not isinstance(zones, list):
        return None
    by_sport = {_dig(zone, "sport"): _dig(zone, "maxHeartRateUsed") for zone in zones}
    return next((by_sport[sport] for sport in ZONE_SPORTS_BY_PREFERENCE
                 if by_sport.get(sport) is not None), None)


def fetch_snapshot(
    client: GarminClient, as_of: date, days: int, is_male: bool | None = None,
    history_days: int = HISTORY_DAYS,
) -> Snapshot:
    """Snapshot ending at `as_of`.

    Activities are read over `history_days` so the regime can see the athlete's real
    history; volume and load are measured over the shorter `days` window. `is_male` is
    the athlete's declared sex: None leaves the load unknown. Any failure becomes
    GarminReadError: a snapshot is all or nothing.
    """
    try:
        return _read_snapshot(client, as_of, days, is_male, history_days)
    except Exception as failure:  # HTTP, decoding and payload errors share no base type
        raise GarminReadError(
            f"Garmin read failed ({type(failure).__name__}): {failure}") from failure


def _read_snapshot(
    client: GarminClient, as_of: date, days: int, is_male: bool | None, history_days: int
) -> Snapshot:
    start = (as_of - timedelta(days=history_days)).isoformat()
    activities = tuple(_to_activity(raw)
                       for raw in client.activities(start, as_of.isoformat()))
    resting = _latest_daily(client.daily_heart_rate, as_of, "restingHeartRate")
    profile = AthleteProfile(resting_hr=resting,
                             max_hr=_max_heart_rate(client.heart_rate_zones()),
                             is_male=is_male)
    acute, chronic = acute_chronic_load(activities, as_of, profile)
    return Snapshot(
        as_of=as_of,
        activities=activities,
        chronic_load=chronic,
        acute_load=acute,
        resting_hr=resting,
        hrv_status=_latest_daily(client.hrv, as_of, "hrvSummary", "status"),
        window_days=days,
        history_days=history_days,
    )


def _bearer_token(session: requests.Session, key: str) -> str:
    """Exchange the key for a token. No message built here contains the key."""
    try:
        response = session.post(BASE_URL + TOKEN_PATH, timeout=REQUEST_TIMEOUT_S,
                                headers={"Accept": "*/*", "Authorization": f"ApiKey {key}"})
    except requests.RequestException as failure:
        raise GarminAuthError(
            f"Garmin could not be reached for a token ({type(failure).__name__})") from failure
    if response.status_code in REFUSED_KEY_STATUSES:
        raise GarminAuthError(
            f"Garmin refused {API_KEY_VARIABLE} (HTTP {response.status_code}): it has "
            f"expired or been revoked. Create a new one on Garmin Connect Web, page "
            f"Personal API Key, and put it in .env.")
    if not response.ok:
        raise GarminAuthError(f"Garmin token request failed (HTTP {response.status_code})")
    token = response.text.strip()
    if not token:
        raise GarminAuthError("Garmin answered the token request with an empty token")
    return token


def build_client() -> PersonalApiClient:
    """Personal API client authenticated with this athlete's key.

    A new token on every run: one request, and no token cache that two athletes on the
    same computer could end up sharing. A missing key raises MissingSettingError with
    the variable name; a refused one GarminAuthError. Both are reported by the CLI,
    never as a traceback.
    """
    key = required_setting(API_KEY_VARIABLE)
    session = requests.Session()
    session.headers.update({"Accept": "application/json",
                            "Authorization": f"Bearer {_bearer_token(session, key)}"})
    return PersonalApiClient(session)
