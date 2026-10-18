from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable


@dataclass
class TimelineItem:
    """One scheduled block in a profile's timeline."""

    item_id: str
    label: str
    profile_id: str
    start: datetime
    end: datetime
    status: str = "planned"
    lane: int = field(default=0, init=False)


def build_timeline_items(
    events: Iterable,
    horizon_hours: float,
    now: datetime | None = None,
) -> tuple[list[TimelineItem], datetime, datetime]:
    """Create visible schedule blocks without involving Qt or external I/O."""

    current = now or datetime.now(timezone.utc)
    window_start = current - timedelta(hours=horizon_hours)
    window_end = current + timedelta(hours=horizon_hours)
    items: list[TimelineItem] = []
    for event in events:
        is_init = bool(event.is_init)
        start = (
            event._parse_datetime(event.last_started_at)
            if event.status == "running" or is_init
            else event._parse_datetime(event.next_due_at)
        )
        if start is None:
            continue
        duration = event.average_duration_seconds or 15 * 60
        end = start + timedelta(seconds=duration)
        if event.status == "running":
            end = max(current, end)
        elif is_init:
            finished = event._parse_datetime(event.last_finished_at)
            if finished is not None:
                end = finished
        if end < window_start or start > window_end:
            continue
        task_start = start
        tasks = [
            task
            for task in event.ordered_tasks()
            if task.active
            and (not is_init or task.last_started_at)
        ]
        if not tasks:
            tasks = [
                task
                for task in event.ordered_tasks()
                if not is_init or task.last_started_at
            ]
        for task in tasks:
            task_started = event._parse_datetime(task.last_started_at) if is_init else None
            task_finished = event._parse_datetime(task.last_finished_at) if is_init else None
            if task_started is not None:
                task_start = task_started
            duration = task.average_duration_seconds or event.average_duration_seconds
            duration = duration or 15 * 60
            task_end = task_finished or task_start + timedelta(seconds=duration)
            if event.status == "running" and task.status == "running":
                task_end = max(current, task_end)
            if task_end >= window_start and task_start <= window_end:
                label = task.name or event.name or event.event_id
                if is_init:
                    label = f"Init: {label}"
                items.append(
                    TimelineItem(
                        item_id=task.task_id or event.event_id,
                        label=label,
                        profile_id=event.profile_id,
                        start=task_start,
                        end=task_end,
                        status=(
                            "running"
                            if task.status == "running"
                            else event.status if is_init else "planned"
                        ),
                    )
                )
            if not is_init:
                task_start = task_end
    return assign_lanes(items), window_start, window_end


def _contains(first: TimelineItem, second: TimelineItem) -> bool:
    return (
        first.start <= second.start
        and first.end >= second.end
        and (first.start < second.start or first.end > second.end)
    )


def assign_lanes(items: list[TimelineItem]) -> list[TimelineItem]:
    """Keep partial overlaps together, but separate fully covered blocks.

    Items are returned in next-execution order. Lanes are assigned per profile
    so one account cannot hide another account's schedule.
    """

    ordered = sorted(items, key=lambda item: (item.profile_id, item.start, item.end, item.item_id))
    lanes_by_profile: dict[str, list[list[TimelineItem]]] = {}
    for item in ordered:
        profile_lanes = lanes_by_profile.setdefault(item.profile_id, [])
        for lane_index, lane_items in enumerate(profile_lanes):
            if not any(_contains(existing, item) or _contains(item, existing) for existing in lane_items):
                item.lane = lane_index
                lane_items.append(item)
                break
        else:
            item.lane = len(profile_lanes)
            profile_lanes.append([item])
    return ordered