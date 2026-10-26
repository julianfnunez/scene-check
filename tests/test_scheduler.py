import asyncio
import json
import threading
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from scheduler import due_schedules, occurrence, run_scheduler, submit_due_checks
from service import CheckService, BusyError, DuplicateScheduledError, PlanError
from settings import Configuration, Schedule, load_config


def config(**schedule_changes):
    return Configuration.model_validate({
        'sensors': {'cam': {'name': 'Camera'}},
        'scenes': {'coop': {'name': 'Coop', 'sensors': {'cam': {}}, 'conditions': ['Three chickens visible']}},
        'schedules': {'nightly': {'scene': 'coop', 'time': '21:15', 'timezone': 'America/New_York',
                                'catch_up_minutes': 60, **schedule_changes}},
    })


def utc(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


@pytest.mark.parametrize('changes', [
    {'time': '25:00'}, {'time': '21:60'}, {'time': '9:15'}, {'timezone': 'Not/A_Zone'},
    {'scene': 'missing'}, {'catch_up_minutes': 0}, {'catch_up_minutes': 1441},
])
def test_invalid_schedule(changes):
    with pytest.raises(ValueError): config(**changes)


@pytest.mark.parametrize('stamp,expected', [
    ('2026-10-16T21:14:59-04:00', []),
    ('2026-10-16T21:15:00-04:00', ['2026-10-16']),
    ('2026-10-16T22:14:59-04:00', ['2026-10-16']),
    ('2026-10-16T22:15:00-04:00', []),
    ('2026-10-17T03:00:00-04:00', []),
])
def test_due_window_boundaries(stamp, expected):
    assert [d[2] for d in due_schedules(config(), utc(stamp))] == expected


def test_disabled_and_midnight_catch_up():
    now = utc('2026-10-17T00:10:00-04:00')
    assert [d[2] for d in due_schedules(config(time='23:30'), now)] == ['2026-10-16']
    assert due_schedules(config(time='23:30', enabled=False), now) == []


def test_dst_first_fold_and_spring_gap():
    schedule = Schedule(scene='coop', time='01:30', timezone='America/New_York')
    assert occurrence(schedule, date(2026, 11, 1)) == utc('2026-11-01T05:30:00Z')
    schedule.time = '02:30'
    assert occurrence(schedule, date(2026, 3, 8)) == utc('2026-03-08T07:30:00Z')
    assert due_schedules(config(time='01:30'), utc('2026-11-01T06:30:00Z')) == []


def test_busy_waits_then_submits_and_survives_restart(tmp_path):
    async def exercise():
        state = SimpleNamespace(config=config(), config_error=None, active='manual', task=None)
        runs = []
        def save(rec):
            (tmp_path / (rec['id'] + '.json')).write_text(json.dumps(rec))
        async def runner(rec, scene):
            runs.append(rec['id'])
            rec.update(state='finished', status='unknown')
            save(rec)
            state.active = None
        service = CheckService(state, save, runner, [])
        now = utc('2026-10-16T21:45:00-04:00')
        submit_due_checks(service, now)
        assert not list(tmp_path.glob('*.json'))
        state.active = None
        submit_due_checks(service, now)
        assert state.active is not None
        with pytest.raises(BusyError): service.submit('coop')
        await state.task
        submit_due_checks(service, now)
        records = [json.loads(p.read_text()) for p in tmp_path.glob('*.json')]
        assert len(records) == 1 and records[0]['source'] == 'scheduled'
        assert records[0]['scheduled_date'] == '2026-10-16'
        restored = CheckService(state, save, runner, records)
        submit_due_checks(restored, now)
        assert len(runs) == 1
        with pytest.raises(DuplicateScheduledError):
            restored.submit('coop', schedule_id='nightly', scheduled_date='2026-10-16')
        state.config_error = 'Invalid plan'
        submit_due_checks(restored, utc('2026-10-17T21:15:00-04:00'))
        with pytest.raises(PlanError): restored.submit('coop')
    asyncio.run(exercise())


def test_manual_does_not_claim_schedule_and_save_failure_does_not_reserve():
    async def exercise():
        state = SimpleNamespace(config=config(), config_error=None, active=None, task=None)
        records = []
        async def runner(rec, scene): state.active = None
        service = CheckService(state, records.append, runner, [])
        assert service.submit('coop')['source'] == 'manual'
        await state.task
        submit_due_checks(service, utc('2026-10-16T21:15:00-04:00'))
        await state.task
        assert [r['source'] for r in records] == ['manual', 'scheduled']
        def fail(rec): raise OSError('Disk full')
        broken = CheckService(state, fail, runner, [])
        with pytest.raises(OSError):
            broken.submit('coop', schedule_id='nightly', scheduled_date='2026-10-17')
        assert state.active is None and broken.scheduled_attempts == set()
    asyncio.run(exercise())


def test_yesterdays_attempt_does_not_block_todays_schedule():
    async def exercise():
        state = SimpleNamespace(config=config(), config_error=None, active=None, task=None)
        records = []
        async def runner(rec, scene):
            state.active = None
        service = CheckService(state, records.append, runner, [])
        for stamp in ['2026-10-24T21:15:00-04:00', '2026-10-25T21:15:00-04:00']:
            submit_due_checks(service, utc(stamp))
            await state.task
            submit_due_checks(service, utc(stamp))
        assert [r['scheduled_date'] for r in records] == ['2026-10-24', '2026-10-25']
    asyncio.run(exercise())


def test_background_loop_stops_promptly():
    async def exercise():
        state = SimpleNamespace(config=config(enabled=False), config_error=None)
        stop = asyncio.Event()
        task = asyncio.create_task(run_scheduler(SimpleNamespace(state=state), stop, interval=60))
        await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=.5)
    asyncio.run(exercise())


def test_scheduled_web_check_uses_same_results_actions_and_busy_guard(tmp_path, monkeypatch):
    import web
    c = config()
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(web, 'load_config', lambda: c)
    async def no_plans(c): return c
    monkeypatch.setattr(web, 'resolve_plans', no_plans)
    monkeypatch.setattr(web, 'resolve_action_plans', no_plans)
    release = threading.Event()
    evaluations, actions = [], []
    async def evaluate(scene, configuration, progress):
        evaluations.append(scene.name)
        while not release.is_set(): await asyncio.sleep(.005)
        return {'status': 'normal', 'summary': 'All good', 'conditions': []}
    async def run_actions(rec, scene, configuration, save):
        actions.append(rec['source'])
        rec['actions'] = [{'status': 'succeeded'}]
        save(rec)
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'run_actions', run_actions)
    async def scheduler(service, stop):
        await run_scheduler(service, stop, clock=lambda: utc('2026-10-16T21:15:00-04:00'), interval=.01)
    monkeypatch.setattr(web, 'run_scheduler', scheduler)
    with TestClient(web.app) as client:
        data = client.get('/api/scenes').json()
        assert data['schedules']['nightly']['time'] == '21:15'
        assert client.post('/api/scenes/coop/checks').status_code == 409
        release.set()
        for _ in range(200):
            checks = client.get('/api/checks').json()
            if checks and checks[0]['state'] == 'finished': break
            time.sleep(.01)
        else: pytest.fail('Scheduled check did not finish')
        assert checks[0]['source'] == 'scheduled'
        assert checks[0]['actions'][0]['status'] == 'succeeded'
    with TestClient(web.app) as client:
        assert len(client.get('/api/checks').json()) == 1
        assert evaluations == ['Coop'] and actions == ['scheduled']


def test_split_yaml_loads_schedule(tmp_path, monkeypatch):
    scene_file, sensor_file = tmp_path / 'scenes.yaml', tmp_path / 'sensors.yaml'
    c = config().model_dump()
    scene_file.write_text(json.dumps({k: c[k] for k in ('scenes', 'schedules')}))
    sensor_file.write_text(json.dumps({k: c[k] for k in ('sensors', 'servers')}))
    monkeypatch.setenv('SCENES_FILE', str(scene_file))
    monkeypatch.setenv('SENSORS_FILE', str(sensor_file))
    assert load_config().schedules['nightly'].scene == 'coop'
