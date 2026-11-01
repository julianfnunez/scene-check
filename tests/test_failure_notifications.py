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
                            'conditions': ['Birds visible']}},
        'run': {'on': {'failure': ['notify']}},
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


@pytest.fixture
def runtime(monkeypatch):
    c = config()
    web.app.state.config = c
    web.app.state.active = 'running'
    record = {'id': 'running', 'scene_id': 'coop', 'scene_name': 'Coop',
              'scene_ids': ['coop'], 'scenes': [], 'conditions': [],
              'created_at': 'now', 'state': 'running', 'status': 'unknown'}
    delivered = []
    async def evaluate(*_):
        return {'status': 'normal', 'summary': 'OK', 'conditions': []}
    async def writer(_):
        pytest.fail('Emergency notifications must not call the model writer')
    async def executor(action_id, effective, context):
        from actions import render
        delivered.append(render(effective.actions[action_id].arguments, context)['message'])
        return {'action_id': action_id, 'status': 'succeeded'}
    async def actions(*args, **kwargs):
        kwargs.setdefault('writer', writer)
        return await run_actions(*args, executor=executor, **kwargs)
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'save', lambda _: None)
    monkeypatch.setattr(web, 'run_actions', actions)
    return c, record, delivered


@pytest.mark.parametrize('phase', ['evaluate', 'save'])
def test_runtime_failure_sends_manual_check_message_without_disk_or_model(monkeypatch, runtime, phase):
    c, record, delivered = runtime
    # The emergency path must also work with the normal report template.
    c.actions['notify'].arguments['message'] = '{{ result.plain_english }}'
    if phase == 'evaluate':
        async def evaluate(*_):
            raise RuntimeError('Evaluation failed')
        monkeypatch.setattr(web, 'evaluate', evaluate)
    else:
        def save(_):
            raise OSError('Cannot save')
        monkeypatch.setattr(web, 'save', save)
    async def check():
        if phase == 'save':
            with pytest.raises(OSError): await web.run_check(record, ['coop'])
        else:
            await web.run_check(record, ['coop'])
    asyncio.run(check())
    assert delivered == ['Could not complete the check for Coop. Please check manually.']
    assert web.app.state.active is None
    if phase == 'evaluate':
        assert record['scenes'][0]['error_type'] == 'RuntimeError'


def test_evaluator_exception_finishes_other_scenes_then_alerts_once(monkeypatch, runtime):
    c, record, delivered = runtime
    c.scenes['other'] = c.scenes['coop'].model_copy(update={'name': 'Other'})
    record['scene_ids'] = ['coop', 'other']
    evaluated = []
    async def evaluate(scene, *_):
        evaluated.append(scene.name)
        if scene.name == 'Coop':
            raise RuntimeError('Evaluation failed')
        return {'status': 'normal', 'summary': 'OK', 'conditions': []}
    monkeypatch.setattr(web, 'evaluate', evaluate)
    asyncio.run(web.run_check(record, record['scene_ids']))
    assert evaluated == ['Coop', 'Other']
    assert [s['status'] for s in record['scenes']] == ['unknown', 'normal']
    assert delivered == ['Could not complete the check for Coop, Other. Please check manually.']


def test_completed_unknown_uses_result_policy_instead_of_emergency(monkeypatch, runtime):
    c, record, delivered = runtime
    c.run.on.unknown = ['notify']
    async def evaluate(*_):
        return {'status': 'unknown', 'summary': 'Could not see clearly', 'conditions': []}
    monkeypatch.setattr(web, 'evaluate', evaluate)
    asyncio.run(web.run_check(record, ['coop']))
    assert delivered == ['0/1 scenes normal.']
    assert not record.get('failure_alerted')


@pytest.mark.parametrize('phase', ['before_publish', 'after_publish', 'final_save'])
def test_storage_failure_sends_emergency_only_before_publish(monkeypatch, runtime, phase):
    c, record, delivered = runtime
    c.run.on.normal = ['notify']
    failed = False
    def save(current):
        nonlocal failed
        actions = current.get('actions', [])
        target = (phase == 'before_publish' and actions and actions[-1]['status'] == 'uncertain'
                  or phase == 'after_publish' and actions and actions[-1]['status'] == 'succeeded'
                  or phase == 'final_save' and current['state'] == 'finished')
        if target and not failed:
            failed = True
            raise OSError('Cannot save')
    monkeypatch.setattr(web, 'save', save)
    async def check():
        if phase == 'final_save':
            with pytest.raises(OSError):
                await web.run_check(record, ['coop'])
        else:
            await web.run_check(record, ['coop'])
    asyncio.run(check())
    assert failed
    assert delivered == (['Could not complete the check for Coop. Please check manually.']
                         if phase == 'before_publish' else ['1/1 scenes normal.'])
    assert record['state'] == 'finished'
    assert web.app.state.active is None


@pytest.mark.parametrize('outcome', ['failed', 'raise'])
def test_unconfirmed_publish_never_triggers_another_notification(monkeypatch, runtime, outcome):
    import actions
    c, record, delivered = runtime
    c.run.on.normal = ['notify']
    async def executor(action_id, effective, context):
        delivered.append(context['result']['summary'])
        if outcome == 'raise':
            raise RuntimeError('Lost response after delivery')
        return {'action_id': action_id, 'status': 'failed'}
    async def run(*args, **kwargs):
        return await actions.run_actions(*args, executor=executor, **kwargs)
    monkeypatch.setattr(web, 'run_actions', run)
    asyncio.run(web.run_check(record, ['coop']))
    assert delivered == ['1/1 scenes normal.']
    assert record['notification_attempted'] is True
    assert web.app.state.active is None
