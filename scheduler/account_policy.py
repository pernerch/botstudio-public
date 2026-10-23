"""Resource-local account fairness and switch-cost-aware batching."""

from dataclasses import dataclass
from datetime import datetime, timezone
from math import log1p

from scheduler.models import Event


@dataclass(frozen=True)
class AccountWork:
    due_tasks: int = 0
    upcoming_tasks: int = 0
    oldest_seconds: float = 0
    next_seconds: float | None = None
    switch_seconds: float = 60
    queued_at: float | None = None

    @classmethod
    def from_events(cls, events, now: datetime, horizon: float):
        due_tasks = upcoming_tasks = 0
        oldest = 0.0
        next_seconds = None
        switch_samples = []
        queued_at = None
        for event in events:
            if event.is_init:
                switch_samples.extend(
                    task.average_duration_seconds * (
                        2 if task.kind == "profile_validation" else 1
                    )
                    for task in event.tasks
                    if task.active and task.duration_samples_seconds
                )
                if not any(
                    task.kind == "profile_switch" and task.duration_samples_seconds
                    for task in event.tasks
                ):
                    switch_samples.append(60.0)
                continue
            if (
                event.is_interrupt or (not event.enabled and event.status != "queued")
                or not event.function_ids
            ):
                continue
            count = sum(task.active for task in event.tasks)
            due = Event._parse_datetime(event.next_due_at)
            if due is not None:
                due = due.replace(tzinfo=due.tzinfo or timezone.utc)
                seconds = (due - now).total_seconds()
            else:
                seconds = None
            if event.status == "queued":
                requested = due.timestamp() if due is not None else float("-inf")
                queued_at = requested if queued_at is None else min(queued_at, requested)
            if event.is_due(now):
                due_tasks += count
                # Retries and activation-only Events must not acquire ancient age.
                if seconds is not None and event.status not in {"failed", "timeout"}:
                    oldest = max(oldest, -seconds)
            elif seconds is not None and 0 < seconds <= horizon:
                upcoming_tasks += count
                next_seconds = seconds if next_seconds is None else min(next_seconds, seconds)
        return cls(
            due_tasks, upcoming_tasks, oldest, next_seconds,
            max(1.0, sum(switch_samples)) if switch_samples else 60.0,
            queued_at,
        )


class AccountSchedulingPolicy:
    def __init__(self):
        self.active = {}
        self.active_since = {}
        self.waiting_since = {}
        self.last_served = {}
        self.reasons = {}

    @staticmethod
    def identity(profile):
        return str(profile.get("botstudio_profile_id") or profile.get("id") or "")

    @staticmethod
    def horizon(settings, switch_seconds):
        return min(3600.0, max(
            settings["scheduling_lookahead_seconds"], 2 * switch_seconds,
        ))

    def activate(self, profile, now):
        instance = str(profile.get("emulator_instance"))
        identity = self.identity(profile)
        if self.active.get(instance) != identity:
            self.active[instance] = identity
            self.active_since[instance] = now

    def served(self, profile, now):
        identity = self.identity(profile)
        self.last_served[identity] = now
        self.waiting_since.pop(identity, None)

    def reset_instance(self, instance):
        instance = str(instance)
        identity = self.active.pop(instance, None)
        self.active_since.pop(instance, None)
        if identity is not None:
            self.waiting_since.pop(identity, None)
            self.last_served.pop(identity, None)

    def choose(self, profiles, work, settings, now):
        if not profiles:
            return None
        instance = str(profiles[0].get("emulator_instance"))
        by_id = {self.identity(profile): profile for profile in profiles}
        active_id = self.active.get(instance)
        current = by_id.get(active_id)
        due = [identity for identity in by_id if work[identity].due_tasks]
        for identity in by_id:
            if identity not in due:
                self.waiting_since.pop(identity, None)
            else:
                since = max(
                    now - work[identity].oldest_seconds,
                    self.last_served.get(identity, float("-inf")),
                )
                self.waiting_since.setdefault(identity, since)

        def score(identity):
            item = work[identity]
            priority = int(by_id[identity].get("priority", 50) or 0)
            bundle = min(12, item.due_tasks + item.upcoming_tasks * 0.25)
            return priority + 18 * log1p(bundle) + min(40, item.oldest_seconds / 180)

        def ranked(identities):
            return max(identities, key=lambda identity: (
                score(identity), work[identity].oldest_seconds, identity,
            ))

        def selected(identity, reason):
            self.reasons[instance] = reason
            return identity

        queued = [identity for identity in due if work[identity].queued_at is not None]
        if queued:
            return selected(min(
                queued, key=lambda identity: (work[identity].queued_at, identity),
            ), "manual_request")

        if not due:
            # Never switch merely to wait for a lone future Task.
            if current and work[active_id].upcoming_tasks:
                return selected(active_id, "current_account_due_soon")
            if len(profiles) == 1 and work[next(iter(by_id))].upcoming_tasks:
                return selected(next(iter(by_id)), "only_account_due_soon")
            upcoming = [
                identity for identity in by_id
                if work[identity].upcoming_tasks >= 2
            ]
            if upcoming:
                target = min(upcoming, key=lambda identity: (
                    work[identity].next_seconds, -score(identity), identity,
                ))
                return selected(target, "prepare_upcoming_bundle")
            return selected(active_id if current else None, "no_due_work")

        if current is None:
            return selected(ranked(due), "priority_backlog_age")

        others = [identity for identity in due if identity != active_id]
        if not others:
            return selected(active_id, "current_account_has_work")

        current_work = work[active_id]
        elapsed = now - self.active_since.get(instance, now)
        # The protected useful-work window is cost-related, but always bounded.
        minimum_dwell = min(600.0, max(120.0, current_work.switch_seconds * 2))
        if settings[active_id]["limit_profile_active_time"]:
            minimum_dwell = min(
                minimum_dwell, settings[active_id]["max_active_minutes"] * 60,
            )
        if current_work.due_tasks and elapsed < minimum_dwell:
            return selected(active_id, "minimum_useful_work_window")

        expired = [
            identity for identity in others
            if now - self.waiting_since[identity]
            >= settings[identity]["scheduling_max_wait_minutes"] * 60
        ]
        if expired:
            target = max(expired, key=lambda identity: (
                now - self.waiting_since[identity], score(identity), identity,
            ))
            return selected(target, "maximum_wait_reached")

        active_settings = settings[active_id]
        if (
            active_settings["limit_profile_active_time"]
            and elapsed >= active_settings["max_active_minutes"] * 60
        ):
            return selected(ranked(others), "maximum_active_time_reached")

        target = ranked(others)
        if not current_work.due_tasks:
            if (
                current_work.upcoming_tasks
                and work[target].due_tasks + work[target].upcoming_tasks < 2
                and score(target) <= score(active_id) + 20
            ):
                return selected(active_id, "avoid_round_trip_before_near_due")
            return selected(target, "current_account_drained")

        # A single ordinary Task never causes a costly mid-batch switch.
        margin = 20 + min(40, work[target].switch_seconds / 6)
        if (
            work[target].due_tasks + work[target].upcoming_tasks >= 2
            and score(target) > score(active_id) + margin
        ):
            return selected(target, "backlog_benefit_exceeds_switch_cost")
        return selected(active_id, "retain_current_batch")
