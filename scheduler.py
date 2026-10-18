"""Daily timezone-aware scheduling through the shared submission service."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from service import BusyError, PlanError, DuplicateScheduledError

logger = logging.getLogger(__name__)


def occurrence(schedule, date):
    hour, minute = map(int, schedule.time.split(':'))
    local = datetime(date.year, date.month, date.day, hour, minute,
                     tzinfo=ZoneInfo(schedule.timezone), fold=0)
    # Fall-back uses the first occurrence. A spring-forward gap shifts forward by the gap.
    return local.astimezone(timezone.utc)


def due_schedules(config, now):
    if now.tzinfo is None:
        raise ValueError('Scheduler clock must include a timezone')
    due = []
    for schedule_id, schedule in config.schedules.items():
        if not schedule.enabled:
            continue
        today = now.astimezone(ZoneInfo(schedule.timezone)).date()
        for date in (today - timedelta(days=1), today):
            scheduled = occurrence(schedule, date)
            if scheduled <= now < scheduled + timedelta(minutes=schedule.catch_up_minutes):
                due.append((scheduled, schedule_id, date.isoformat(), schedule.scene))
    return sorted(due)


def submit_due_checks(service, now):
    if service.state.config_error:
        return
    for scheduled, schedule_id, date, scene in due_schedules(service.state.config, now):
        try:
            service.submit(scene, schedule_id=schedule_id, scheduled_date=date,
                           scheduled_for=scheduled.isoformat())
        except DuplicateScheduledError:
            continue
        except (BusyError, PlanError):
            return  # Retry next tick, provided the occurrence remains within its window.


async def run_scheduler(service, stop, *, clock=lambda: datetime.now(timezone.utc), interval=5):
    while not stop.is_set():
        try:
            submit_due_checks(service, clock())
        except Exception:
            logger.exception('Scheduler tick failed')
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
