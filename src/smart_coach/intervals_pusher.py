"""Publish a planned week to intervals.icu, which syncs it to Garmin officially.

The calendar is made to match the plan from today on: a session is created, updated in
place when its content changed, and removed when the plan no longer holds it. Only
events this program created are ever touched — they carry an external id with our
prefix — and nothing dated before today is rewritten.

Field names follow the documented Swagger contract; confirm them against the live API
before relying on the first real push.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import requests

from smart_coach.models import Week, Workout

BASE_URL = "https://intervals.icu/api/v1/athlete/{athlete_id}/events"
TIMEOUT_S = 30
EXTERNAL_ID_PREFIX = "smart-coach:"
LAST_DAY_OF_WEEK = 6

Event = dict[str, Any]


@dataclass(frozen=True)
class PushResult:
    created: int
    skipped: int
    error: str | None
    updated: int = 0
    removed: int = 0


REPEAT_MARKER = re.compile(r"^\d+x$", re.IGNORECASE)


def workout_description(workout: Workout, easy_step_target: str) -> str:
    """Render the workout as intervals.icu workout-builder text.

    Step text is passed through untouched — `m` means minutes there and `mtr` means
    metres, so reinterpreting a line would silently change the session. The only
    transformation is the blank-line framing the builder requires around a repeat block.
    A workout with no steps becomes a single distance step carrying `easy_step_target`:
    every documented step has a target, and a WORKOUT event whose description does not
    parse as steps is rejected, so omitting it would be an unverified shape.
    """
    if not workout.steps:
        return f"- {workout.total_km:g}km {easy_step_target}"

    lines: list[str] = []
    previous_depth = 0
    for step in workout.steps:
        if REPEAT_MARKER.match(step.text) and lines:
            lines.append("")
        elif previous_depth > step.depth:
            lines.append("")
        lines.append(step.text)
        previous_depth = step.depth
    return "\n".join(lines)


def _payload(workout: Workout, easy_step_target: str) -> Event:
    return {
        "start_date_local": f"{workout.day.isoformat()}T00:00:00",
        "category": "WORKOUT",
        "type": "Run",
        "name": workout.name,
        "description": workout_description(workout, easy_step_target),
    }


def _external_id(day: date, position: int) -> str:
    """Stable identity of a planned session: its day and its position within that day.

    Not its name or its content: rewriting or renaming a session must update the event
    on the watch, not leave the old one there or add a second one beside it.
    """
    return f"{EXTERNAL_ID_PREFIX}{day.isoformat()}:{position}"


def _desired(week: Week, easy_step_target: str, today: date) -> dict[str, Event]:
    """What the calendar should hold from `today` on, by external id."""
    sessions_on: Counter[date] = Counter()
    events: dict[str, Event] = {}
    for workout in week.workouts:
        external_id = _external_id(workout.day, sessions_on[workout.day])
        sessions_on[workout.day] += 1
        if workout.day >= today:
            events[external_id] = {**_payload(workout, easy_step_target),
                                   "external_id": external_id}
    return events


def _published(session: requests.Session, url: str, week: Week,
               today: date) -> dict[str, Event]:
    """Events this program created in the week, from `today` on, by external id."""
    monday = date.fromisocalendar(week.iso_year, week.iso_week, 1)
    oldest, newest = max(monday, today), monday + timedelta(days=LAST_DAY_OF_WEEK)
    if oldest > newest:
        return {}
    response = session.get(
        url, params={"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        timeout=TIMEOUT_S)
    response.raise_for_status()
    return {event["external_id"]: event for event in response.json()
            if str(event.get("external_id") or "").startswith(EXTERNAL_ID_PREFIX)}


def _differs(event: Event, payload: Event) -> bool:
    return (event.get("name") != payload["name"]
            or event.get("description") != payload["description"])


def _sync(session: requests.Session, url: str, desired: dict[str, Event],
          published: dict[str, Event], tally: Counter[str]) -> None:
    """Create, update and remove until `published` matches `desired`. The tally is
    updated after each call that landed, so a failure midway reports what was done."""
    for external_id, payload in desired.items():
        event = published.get(external_id)
        if event is None:
            session.post(url, json=payload, timeout=TIMEOUT_S).raise_for_status()
            tally["created"] += 1
        elif _differs(event, payload):
            session.put(f"{url}/{event['id']}", json=payload,
                        timeout=TIMEOUT_S).raise_for_status()
            tally["updated"] += 1
        else:
            tally["skipped"] += 1
    for external_id, event in published.items():
        if external_id not in desired:
            session.delete(f"{url}/{event['id']}", timeout=TIMEOUT_S).raise_for_status()
            tally["removed"] += 1


def _result(tally: Counter[str], error: str | None) -> PushResult:
    return PushResult(created=tally["created"], skipped=tally["skipped"], error=error,
                      updated=tally["updated"], removed=tally["removed"])


def push_week(
    week: Week, athlete_id: str, api_key: str, easy_step_target: str, *,
    today: date, session: requests.Session | None = None,
) -> PushResult:
    """Make intervals.icu match the week from `today` on."""
    tally: Counter[str] = Counter()
    if not week.workouts:
        # An empty week is a mistake upstream, never an instruction to clear the calendar.
        return _result(tally, None)

    session = session or requests.Session()
    session.auth = ("API_KEY", api_key)
    url = BASE_URL.format(athlete_id=athlete_id)
    try:
        _sync(session, url, _desired(week, easy_step_target, today),
              _published(session, url, week, today), tally)
    except requests.RequestException as failure:
        # Report what did land: claiming zero would make the retry look like a
        # fresh start and hide a partial publish.
        return _result(tally, str(failure))
    return _result(tally, None)
