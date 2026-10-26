"""Shared atomic submission path for REST and daily schedules; one worker only."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4


class BusyError(RuntimeError):
    pass


class PlanError(RuntimeError):
    pass


class DuplicateScheduledError(RuntimeError):
    pass


class CheckService:
    def __init__(self, state, save, runner, records, on_failure=None):
        self.state, self.save, self.runner = state, save, runner
        self.on_failure = on_failure
        self.failure_tasks = set()
        self.failure_alerts = set()
        self.scheduled_attempts = {(r['schedule_id'], r['scheduled_date']) for r in records
                                   if r.get('source') == 'scheduled'}

    def submit(self, scene_id, *, schedule_id=None, scheduled_date=None, scheduled_for=None):
        """No await between checking busy/deduplication and saving/reserving the job."""
        if self.state.config_error:
            raise PlanError(self.state.config_error)
        scene = self.state.config.scenes.get(scene_id)
        if scene is None:
            raise KeyError(scene_id)
        key = (schedule_id, scheduled_date)
        if schedule_id is not None and key in self.scheduled_attempts:
            raise DuplicateScheduledError('This scheduled occurrence was already attempted')
        if self.state.active:
            raise BusyError('A check or action is already running')
        record = {'id': uuid4().hex, 'scene_id': scene_id, 'scene_name': scene.name,
                  'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'running',
                  'status': 'unknown', 'summary': 'Starting check', 'conditions': [],
                  'evidence': [], 'trace': [], 'source': 'scheduled' if schedule_id is not None else 'manual'}
        if schedule_id is not None:
            record.update(schedule_id=schedule_id, scheduled_date=scheduled_date, scheduled_for=scheduled_for)
        if schedule_id is not None and key in self.failure_alerts:
            record['failure_alerted'] = True
        # The durable check record is also the scheduler's claim. Never replay interrupted work.
        try:
            self.save(record)
        except Exception:
            # Retry admission, but alert only once for this scheduled occurrence.
            alert_key = key if schedule_id is not None else record['id']
            if self.on_failure and alert_key not in self.failure_alerts:
                self.failure_alerts.add(alert_key)
                task = asyncio.create_task(self.on_failure(record, scene))
                self.failure_tasks.add(task)
                task.add_done_callback(self.failure_tasks.discard)
            raise
        if schedule_id is not None:
            self.scheduled_attempts.add(key)
        self.state.active = record['id']
        self.state.task = asyncio.create_task(self.runner(record, scene))
        return record
