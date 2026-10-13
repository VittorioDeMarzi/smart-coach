"""Deterministic admissibility checks on a proposed week.

Nothing here asks a model for an opinion: a constraint verified by an LLM is not a
constraint (spec 4.2). Each rule returns violations; an empty result means admissible.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from smart_coach.config import Config
from smart_coach.models import Regime, Severity, Snapshot, Violation, Week


MIN_WORKOUTS_FOR_A_MEANINGFUL_SHARE = 3
DAYS_PER_WEEK = 7


def _severity(value: float, nominal: float, hard: float) -> Severity:
    """WARN between the nominal threshold and the hard one, BLOCK beyond it."""
    return Severity.BLOCK if value > hard else Severity.WARN


def blocking(violations: tuple[Violation, ...]) -> tuple[Violation, ...]:
    """Only the violations that must stop the week."""
    return tuple(v for v in violations if v.severity is Severity.BLOCK)


def _load_is_usable(snapshot: Snapshot, regime: Regime) -> bool:
    """Whether the load rules may be applied at all.

    Three ways the data can fail to support them, all of which must route to the
    absolute caps instead of skipping the check (spec 8.3):
    - the metric is absent;
    - the metric is zero, which is an unworn device, not a rested athlete;
    - the regime is not HOT, so the 28-day chronic window is too short to mean
      anything even though Garmin still reports a number (spec 8.1, 8.2).
    """
    return (regime is Regime.HOT
            and snapshot.chronic_load not in (None, 0)
            and snapshot.acute_load is not None)


def _empty_week(week: Week) -> list[Violation]:
    """A week with no workouts satisfies every other rule by vacuity. Refuse it here,
    or a heading whose workouts were deleted by hand reports success twice."""
    if week.workouts:
        return []
    return [Violation(rule="empty_week",
                      message=f"Week {week.iso_week} contains no workouts.")]


def _no_back_to_back_quality(week: Week, config: Config) -> list[Violation]:
    days = sorted(week.quality_days)
    gap = config.guardrails.min_easy_days_after_quality
    for earlier, later in zip(days, days[1:]):
        if (later - earlier).days <= gap:
            return [Violation(
                rule="no_back_to_back_quality",
                message=(f"Quality sessions on {earlier} and {later} are "
                         f"{(later - earlier).days} day(s) apart; {gap + 1} required."),
            )]
    return []


def _down_week_required(
    week: Week, previous_week: Week | None, weeks_since_down_week: int, config: Config
) -> list[Violation]:
    due = weeks_since_down_week >= config.guardrails.down_week_every
    if not due or previous_week is None:
        return []
    if week.total_km < previous_week.total_km:
        return []
    return [Violation(
        rule="down_week_required",
        message=(f"Week {week.iso_week} is week {weeks_since_down_week} since the last "
                 f"down week and does not reduce volume "
                 f"({week.total_km:.1f} km vs {previous_week.total_km:.1f} km)."),
        severity=Severity.WARN,
    )]


def _planned_km_by_day(weeks: tuple[Week, ...]) -> Counter[date]:
    """Planned kilometres per day, summed: two sessions on one day are both run."""
    planned: Counter[date] = Counter()
    for week in weeks:
        for workout in week.workouts:
            planned[workout.day] += workout.total_km
    return planned


def recent_week_km(
    week: Week, planned_weeks: tuple[Week, ...], snapshot: Snapshot, config: Config
) -> tuple[float, ...]:
    """Running volume of the weeks before `week`, most recent first.

    Each day counts what was run if the snapshot has reached it, what is planned if it
    is still ahead. A day that is neither counts as zero: an unknown week must not
    raise the reference, so missing data can only make the ramp rule stricter.
    """
    planned = _planned_km_by_day(planned_weeks)
    monday = date.fromisocalendar(week.iso_year, week.iso_week, 1)

    def day_km(day: date) -> float:
        if day <= snapshot.as_of:
            return snapshot.running_km_on(day)
        return planned[day]

    return tuple(
        sum(day_km(monday - timedelta(weeks=back) + timedelta(days=n))
            for n in range(DAYS_PER_WEEK))
        for back in range(1, config.guardrails.volume_reference_weeks + 1))


def expected_week_km(week: Week, snapshot: Snapshot) -> float:
    """What the week will amount to: the part already run plus the part still planned.

    A plan written mid-week holds only the days left, and judging those alone ignores
    the kilometres already in the legs. Today counts as the larger of run and planned,
    because the session may not have happened yet; a planned session whose day passed
    without a run counts for nothing.
    """
    as_of = snapshot.as_of
    monday = date.fromisocalendar(week.iso_year, week.iso_week, 1)
    days = [monday + timedelta(days=n) for n in range(DAYS_PER_WEEK)]
    ran_before_today = sum(snapshot.running_km_on(day) for day in days if day < as_of)
    ran_today = snapshot.running_km_on(as_of) if as_of in days else 0.0
    planned_today = sum(w.total_km for w in week.workouts if w.day == as_of)
    still_planned = sum(w.total_km for w in week.workouts if w.day > as_of)
    return ran_before_today + max(ran_today, planned_today) + still_planned


def _previous_week_km(previous_week: Week | None, snapshot: Snapshot) -> float:
    """The smaller of planned and actually run.

    Comparing the plan against the plan is circular: a week the athlete only half
    completed would licence a rise measured from volume they never ran.
    """
    if previous_week is None:
        return 0.0
    actual = snapshot.running_km_in_iso_week(previous_week.iso_year,
                                            previous_week.iso_week)
    return previous_week.total_km if actual is None \
        else min(previous_week.total_km, actual)


def _weekly_volume_increase(
    week: Week, previous_week: Week | None, snapshot: Snapshot, config: Config,
    planned_weeks: tuple[Week, ...],
) -> list[Violation]:
    """Ramp check against the level the athlete has recently sustained.

    The reference is the larger of last week and the recent average, so a down week
    or a fortnight away does not restart the ladder, and the first week of a plan
    (no predecessor) is held to the same rule as every other. Past the nominal ramp the
    week is flagged; it is refused only when it also exceeds every recent week, because
    returning to a level already run is a judgement call and surpassing it is not.
    """
    recent = recent_week_km(week, planned_weeks, snapshot, config)
    reference = max(_previous_week_km(previous_week, snapshot),
                    sum(recent) / len(recent))
    if reference == 0:
        return []  # nothing run or planned to measure from; the absolute cap governs
    rules = config.guardrails
    volume = expected_week_km(week, snapshot)
    nominal = reference * (1 + rules.weekly_volume_increase_max_pct / 100)
    if volume <= nominal:
        return []
    hard = max(reference * (1 + rules.weekly_volume_increase_block_pct / 100),
               max(recent))
    return [Violation(
        rule="weekly_volume_increase",
        message=(f"Volume is {volume:.1f} km against a recent level of "
                 f"{reference:.1f} km: flagged above {nominal:.1f} km, "
                 f"refused above {hard:.1f} km."),
        severity=_severity(volume, nominal, hard),
    )]


def recent_longest_run_km(
    week: Week, planned_weeks: tuple[Week, ...], snapshot: Snapshot, config: Config
) -> float:
    """Longest single run in the days before `week`; zero when there is none.

    Same split as `recent_week_km`: a run counts as recorded up to the snapshot and as
    planned after it, so a long run written in the plan and never run licenses nothing.
    """
    monday = date.fromisocalendar(week.iso_year, week.iso_week, 1)
    earliest = monday - timedelta(days=config.guardrails.long_run_reference_days)
    day_before = monday - timedelta(days=1)
    first_day_ahead = max(earliest, snapshot.as_of + timedelta(days=1))
    ran = snapshot.longest_run_km_between(earliest, min(day_before, snapshot.as_of))
    ahead = (workout.total_km for planned in planned_weeks for workout in planned.workouts
             if first_day_ahead <= workout.day <= day_before)
    return max(ran, max(ahead, default=0.0))


def _long_run_spike(
    week: Week, snapshot: Snapshot, config: Config, planned_weeks: tuple[Week, ...]
) -> list[Violation]:
    """The week's longest run against the longest run of the recent past.

    This is the measure with evidence behind it: in a cohort of 5,205 Garmin users
    (Br J Sports Med, 2025) injury rate rose once a single session exceeded the longest
    run of the previous 30 days by more than 10%, while week-to-week change and the
    acute/chronic ratio showed no clear association.
    """
    reference = recent_longest_run_km(week, planned_weeks, snapshot, config)
    if reference == 0:
        return []  # no recent run to compare with; the absolute long-run cap governs
    rules = config.guardrails
    nominal = reference * (1 + rules.long_run_increase_max_pct / 100)
    if week.longest_run_km <= nominal:
        return []
    hard = reference * (1 + rules.long_run_increase_block_pct / 100)
    return [Violation(
        rule="long_run_spike",
        message=(f"Longest run is {week.longest_run_km:.1f} km against "
                 f"{reference:.1f} km in the last {rules.long_run_reference_days} days: "
                 f"flagged above {nominal:.1f} km, refused above {hard:.1f} km."),
        severity=_severity(week.longest_run_km, nominal, hard),
    )]


def _long_run_share(week: Week, config: Config) -> list[Violation]:
    """Flag a week that is almost only its long run. Advisory, never a refusal.

    How long the long run may be is `_long_run_spike`'s decision, and that rule has
    evidence behind it; this share is a rule of thumb. It must not refuse, because the
    most lopsided week of a plan is the legitimate one: a race week. From three runs up
    only: with one or two the share is arithmetic, not a signal.
    """
    if len(week.workouts) < MIN_WORKOUTS_FOR_A_MEANINGFUL_SHARE or week.total_km == 0:
        return []
    share_pct = week.longest_run_km / week.total_km * 100
    cap = config.guardrails.long_run_share_max_pct
    if share_pct <= cap:
        return []
    return [Violation(
        rule="long_run_share",
        message=(f"Long run is {share_pct:.1f}% of weekly volume "
                 f"({week.longest_run_km:.1f} of {week.total_km:.1f} km); "
                 f"flagged above {cap}%."),
        severity=Severity.WARN,
    )]


def _acwr_band(snapshot: Snapshot, regime: Regime, config: Config) -> list[Violation]:
    """Nominal rule. Declines when the load is not usable — `_absolute_volume_cap`
    fires in exactly that case, so declining here never leaves the week unchecked."""
    if not _load_is_usable(snapshot, regime):
        return []
    ratio = snapshot.acute_load / snapshot.chronic_load
    low, high = config.guardrails.acwr_min, config.guardrails.acwr_max
    if low <= ratio <= high:
        return []
    # Below the band means training less than the chronic average: never dangerous,
    # so it can only ever be advisory.
    severity = (_severity(ratio, high, config.guardrails.acwr_block)
                if ratio > high else Severity.WARN)
    return [Violation(
        rule="acwr_band",
        message=f"Acute/chronic load ratio is {ratio:.2f}; band is {low}-{high}.",
        severity=severity,
    )]


def _absolute_volume_cap(
    week: Week, snapshot: Snapshot, regime: Regime, config: Config
) -> list[Violation]:
    """Fail-closed replacement for the load-based rules (spec 8.3).

    Applies in exactly the cases `_load_is_usable` rejects, so the two are complements
    and never both decline.
    """
    if _load_is_usable(snapshot, regime):
        return []
    violations: list[Violation] = []
    # The floor is built from the athlete's history, and spec 8.4 says a long
    # interruption invalidates that history. In COLD it must not loosen the cap.
    floor_weekly = 0.0 if regime is Regime.COLD else config.baseline.floor_weekly_km
    floor_long = 0.0 if regime is Regime.COLD else config.baseline.floor_long_run_km
    km_cap = max(config.guardrails.fallback_weekly_km_cap, floor_weekly)
    long_cap = max(config.guardrails.fallback_long_run_km_cap, floor_long)
    volume = expected_week_km(week, snapshot)
    if volume > km_cap:
        violations.append(Violation(
            rule="absolute_volume_cap",
            message=(f"Load data unavailable: weekly volume capped at {km_cap:.1f} km, "
                     f"proposed {volume:.1f} km."),
        ))
    if week.longest_run_km > long_cap:
        violations.append(Violation(
            rule="absolute_volume_cap",
            message=(f"Load data unavailable: long run capped at {long_cap:.1f} km, "
                     f"proposed {week.longest_run_km:.1f} km."),
        ))
    return violations


def validate_structure(
    week: Week,
    previous_week: Week | None,
    snapshot: Snapshot,
    config: Config,
    weeks_since_down_week: int = 0,
    planned_weeks: tuple[Week, ...] = (),
) -> tuple[Violation, ...]:
    """The rules that judge the shape of the plan, usable on any week of it.

    `planned_weeks` is the rest of the plan: it supplies the volume of weeks that have
    not been run yet.
    """
    empty = _empty_week(week)
    if empty:
        return tuple(empty)
    violations: list[Violation] = []
    violations += _no_back_to_back_quality(week, config)
    violations += _down_week_required(week, previous_week, weeks_since_down_week, config)
    violations += _weekly_volume_increase(week, previous_week, snapshot, config,
                                          planned_weeks)
    violations += _long_run_spike(week, snapshot, config, planned_weeks)
    violations += _long_run_share(week, config)
    return tuple(violations)


def validate(
    week: Week,
    previous_week: Week | None,
    snapshot: Snapshot,
    regime: Regime,
    config: Config,
    weeks_since_down_week: int = 0,
    planned_weeks: tuple[Week, ...] = (),
) -> tuple[Violation, ...]:
    """All violations of the week about to be published. No blocker means it may be.

    Adds to the structural rules the ones that read the athlete's current state, which
    says nothing about a week further ahead.
    """
    structural = validate_structure(week, previous_week, snapshot, config,
                                    weeks_since_down_week, planned_weeks)
    if not week.workouts:
        return structural
    return structural + tuple(_acwr_band(snapshot, regime, config)
                              + _absolute_volume_cap(week, snapshot, regime, config))
