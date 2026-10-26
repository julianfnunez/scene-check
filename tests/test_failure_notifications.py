import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import web
from actions import run_actions
from service import CheckService
from settings import Configuration


def config():
    return Configuration.model_validate({
        'servers': {'alerts': {'command': 'python', 'args': []}},
        'actions': {'notify': {'name': 'Notify', 'server': 'alerts', 'tool': 'publish',
                              'arguments': {'message': '{{ result.summary }}'}}},
        'sensors': {'cam': {'name': 'Camera', 'server': 'alerts'}},
        'scenes': {'coop': {'name': 'Coop', 'sensors': {'cam': {}},
                            'conditions': ['Birds visible'], 'on': {'failure': ['notify']}}},
    })


def test_submission_failure_notifies_once_while_allowing_retries():
    async def check():
        state = SimpleNamespace(config=config(), config_error=None, active=None)
        alert = AsyncMock()
        def fail(_): raise OSError('Cannot save')
        service = CheckService(state, fail, AsyncMock(), [], on_failure=alert)
        for _ in range(2):
            with pytest.raises(OSError):
                service.submit('coop', schedule_id='nightly', scheduled_date='2026-10-26')
        await asyncio.gather(*service.failure_tasks)
        assert alert.await_count == 1
        assert state.active is None
        assert not service.scheduled_attempts
        with pytest.raises(OSError):
            service.submit('coop', schedule_id='nightly', scheduled_date='2026-10-27')
        await asyncio.gather(*service.failure_tasks)
        assert alert.await_count == 2
        service.save = lambda _: None
        recovered = service.submit('coop', schedule_id='nightly', scheduled_date='2026-10-26')
        assert recovered['failure_alerted'] is True
        await state.task
    asyncio.run(check())


@pytest.mark.parametrize('phase', ['evaluate', 'save'])
def test_runtime_failure_sends_manual_check_message_without_disk_or_model(monkeypatch, phase):
    c = config()
    web.app.state.config = c
    web.app.state.active = 'running'
    record = {'id': 'running', 'scene_id': 'coop', 'scene_name': 'Coop',
              'created_at': 'now', 'status': 'unknown'}
    async def evaluate(*_):
        if phase == 'evaluate': raise RuntimeError('Evaluation failed')
        return {'status': 'normal', 'summary': 'OK'}
    def save(_):
        if phase == 'save': raise OSError('Cannot save')
    delivered = []
    async def executor(action_id, effective, context):
        delivered.append(context['result']['summary'])
        return {'status': 'succeeded'}
    async def actions(*args, **kwargs):
        return await run_actions(*args, executor=executor, **kwargs)
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'save', save)
    monkeypatch.setattr(web, 'run_actions', actions)
    async def check():
        if phase == 'save':
            with pytest.raises(OSError): await web.run_check(record, c.scenes['coop'])
        else:
            await web.run_check(record, c.scenes['coop'])
    asyncio.run(check())
    assert delivered == ['Could not complete the check for Coop. Please check manually.']
    assert web.app.state.active is None
