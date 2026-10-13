"""Publishing makes the calendar match the plan, it does not just add to it.

A week is published, read on the watch, and then changed: that is the normal life of a
plan. Matching on (date, name) left the old session on the watch when its content
changed and added a second one when its name did. Each planned session now carries a
stable external id — its day and its position in that day — so it is updated in place,
and a session the plan no longer holds is removed. Only events this program created are
ever touched, and nothing dated before today is rewritten.
"""

import json
from datetime import date

import responses

from smart_coach.intervals_pusher import push_week
from smart_coach.models import Step, Week, Workout

ATHLETE = "i12345"
BASE = f"https://intervals.icu/api/v1/athlete/{ATHLETE}/events"
SUNDAY_BEFORE = date(2026, 9, 27)


def _workout(day: date, name: str, km: float, step: str = "- 5km Z2 Pace") -> Workout:
    return Workout(day=day, name=name, sport="running", steps=(Step(text=step),),
                   total_km=km, is_quality=False)


def _week(*workouts: Workout) -> Week:
    return Week(iso_year=2026, iso_week=40, workouts=workouts or (
        _workout(date(2026, 9, 28), "Easy", 8.0),
        _workout(date(2026, 9, 30), "Fartlek", 10.0),
    ))


def _published(event_id: int, day: str, position: int, name: str,
               description: str = "- 5km Z2 Pace") -> dict:
    return {"id": event_id, "external_id": f"smart-coach:{day}:{position}",
            "start_date_local": f"{day}T00:00:00", "name": name,
            "description": description}


def _push(week: Week, today: date = SUNDAY_BEFORE):
    return push_week(week, ATHLETE, "key", "Z2 Pace", today=today)


def _calls(method: str) -> list:
    return [call.request for call in responses.calls if call.request.method == method]


@responses.activate
def test_creates_one_event_per_workout_each_with_its_own_identity():
    responses.add(responses.GET, BASE, json=[], status=200)
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)

    result = _push(_week())

    assert (result.created, result.updated, result.removed, result.skipped) == (2, 0, 0, 0)
    assert result.error is None
    assert [json.loads(r.body)["external_id"] for r in _calls("POST")] == [
        "smart-coach:2026-09-28:0", "smart-coach:2026-09-30:0"]


@responses.activate
def test_running_twice_changes_nothing():
    responses.add(responses.GET, BASE, status=200, json=[
        _published(11, "2026-09-28", 0, "Easy"),
        _published(12, "2026-09-30", 0, "Fartlek")])

    result = _push(_week())

    assert (result.created, result.updated, result.removed, result.skipped) == (0, 0, 0, 2)
    assert len(responses.calls) == 1, "only the read"


@responses.activate
def test_a_session_whose_content_changed_is_updated_in_place():
    """Same day, same name, new steps: the watch must not keep the old version."""
    responses.add(responses.GET, BASE, status=200, json=[
        _published(11, "2026-09-28", 0, "Easy", description="- 7km 6:10/km Pace")])
    responses.add(responses.PUT, f"{BASE}/11", json={"id": 11}, status=200)

    result = _push(_week(_workout(date(2026, 9, 28), "Easy", 7.0, "- 7km 5:50/km Pace")))

    assert (result.created, result.updated) == (0, 1)
    assert json.loads(_calls("PUT")[0].body)["description"] == "- 7km 5:50/km Pace"


@responses.activate
def test_a_renamed_session_is_updated_not_duplicated():
    responses.add(responses.GET, BASE, status=200, json=[
        _published(11, "2026-09-28", 0, "Easy")])
    responses.add(responses.PUT, f"{BASE}/11", json={"id": 11}, status=200)

    result = _push(_week(_workout(date(2026, 9, 28), "Easy + allunghi", 8.0)))

    assert (result.created, result.updated) == (0, 1)
    assert not _calls("POST")


@responses.activate
def test_a_session_the_plan_no_longer_holds_is_removed():
    """Moved from Monday to Tuesday: one event on the watch, not two."""
    responses.add(responses.GET, BASE, status=200, json=[
        _published(11, "2026-09-28", 0, "Easy")])
    responses.add(responses.POST, BASE, json={"id": 12}, status=200)
    responses.add(responses.DELETE, f"{BASE}/11", status=200)

    result = _push(_week(_workout(date(2026, 9, 29), "Easy", 8.0)))

    assert (result.created, result.removed) == (1, 1)


@responses.activate
def test_events_this_program_did_not_create_are_never_touched():
    """A session the athlete added by hand, or one from another app, is theirs."""
    responses.add(responses.GET, BASE, status=200, json=[
        {"id": 50, "start_date_local": "2026-09-28T00:00:00", "name": "Easy"},
        {"id": 51, "external_id": "other-app:1", "name": "Yoga",
         "start_date_local": "2026-09-29T00:00:00"}])
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)

    result = _push(_week())

    assert (result.created, result.updated, result.removed) == (2, 0, 0)
    assert not _calls("PUT") and not _calls("DELETE")


@responses.activate
def test_days_already_gone_are_not_rewritten():
    """Wednesday of the week: Monday's event stays as it was, whatever the plan says
    now, and Monday's plan entry is not published after the fact."""
    wednesday = date(2026, 9, 30)
    responses.add(responses.GET, BASE, json=[], status=200)
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)

    result = _push(_week(), today=wednesday)

    assert result.created == 1
    assert _calls("GET")[0].params["oldest"] == "2026-09-30"
    assert json.loads(_calls("POST")[0].body)["name"] == "Fartlek"


@responses.activate
def test_two_sessions_on_one_day_keep_separate_identities():
    responses.add(responses.GET, BASE, status=200, json=[
        _published(11, "2026-09-28", 0, "Double")])
    responses.add(responses.POST, BASE, json={"id": 12}, status=200)

    result = _push(_week(_workout(date(2026, 9, 28), "Double", 6.0),
                         _workout(date(2026, 9, 28), "Double", 6.0)))

    assert (result.created, result.skipped) == (1, 1)
    assert json.loads(_calls("POST")[0].body)["external_id"] == "smart-coach:2026-09-28:1"


@responses.activate
def test_an_empty_week_never_wipes_the_calendar():
    result = _push(Week(iso_year=2026, iso_week=40, workouts=()))

    assert (result.created, result.removed, result.error) == (0, 0, None)
    assert len(responses.calls) == 0


@responses.activate
def test_network_failure_is_reported_not_raised():
    """A push failure must never lose the plan, which is already written."""
    responses.add(responses.GET, BASE, status=503)

    result = _push(_week())

    assert result.created == 0
    assert result.error is not None
    assert "503" in result.error


@responses.activate
def test_what_landed_before_a_failure_is_still_reported():
    responses.add(responses.GET, BASE, json=[], status=200)
    responses.add(responses.POST, BASE, json={"id": 1}, status=200)
    responses.add(responses.POST, BASE, status=500)

    result = _push(_week())

    assert result.error is not None
    assert result.created == 1, "the event that did land must be reported"
