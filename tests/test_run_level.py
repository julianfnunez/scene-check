"""Regression tests for batch/single runs and one notification per completed run."""
import asyncio
import time
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest

import web
from actions import run_actions, render
from narration import findings
from scheduler import due_schedules
from settings import Configuration


def config():
    return Configuration.model_validate({
        'servers': {'alerts': {'args': ['-m', 'mcp_servers.alert_server']}},
        'sensors': {'cam': {'name': 'Camera', 'server': 'alerts'}},
        'actions': {'notify': {
            'name': 'Phone', 'server': 'alerts', 'tool': 'publish',
            'arguments': {'message': '{{ result.plain_english }}'},
        }},
        'run': {'on': {'normal': ['notify'], 'not_normal': ['notify']}},
        'scenes': {
            'girls': {'name': 'Girls', 'active': True, 'sensors': {'cam': {}},
                      'conditions': ['Three chickens visible']},
            'chicky': {'name': 'Chicky', 'active': True, 'sensors': {'cam': {}},
                       'conditions': ['One chicken visible']},
            'disabled': {'name': 'Disabled', 'active': False, 'sensors': {'cam': {}},
                         'conditions': ['No dog visible']},
        },
        'schedules': {'nightly': {'target': 'active_scenes', 'time': '21:15'}},
    })


@pytest.mark.parametrize('endpoint,names', [
    ('/api/checks/active', ['Girls', 'Chicky']),
    ('/api/scenes/chicky/checks', ['Chicky']),
    ('/api/scenes/disabled/checks', ['Disabled']),
])
def test_run_notifies_once_for_selected_scenes(tmp_path, monkeypatch, endpoint, names):
    c = config()
    writes, notifications, evaluated = [], [], []
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(web, 'load_config', lambda: c)
    async def no_plans(c): return c
    monkeypatch.setattr(web, 'resolve_plans', no_plans)
    monkeypatch.setattr(web, 'resolve_action_plans', no_plans)

    async def fake_eval(scene, config, progress):
        evaluated.append(scene.name)
        status = 'needs_attention' if scene.name == 'Chicky' else 'normal'
        return {'status': status, 'summary': f'{scene.name} evaluated',
                'conditions': [{'condition': scene.conditions[0],
                                'status': 'not_met' if status == 'needs_attention' else 'met',
                                'explanation': scene.name + ' observed', 'evidence_ids': []}],
                'sensors': [], 'evidence': [], 'trace': []}

    async def fake_writer(record):
        names_seen = [scene['name'] for scene in findings(record)['scenes']]
        writes.append(names_seen)
        return 'Checked: ' + ', '.join(names_seen)

    async def fake_execute(action_id, configuration, context):
        notifications.append(render(configuration.actions[action_id].arguments, context)['message'])
        return {'status': 'succeeded', 'action_id': action_id}

    async def fake_run_actions(record, run, config, save):
        return await run_actions(record, run, config, save, executor=fake_execute,
                                 writer=fake_writer)

    monkeypatch.setattr(web, 'evaluate', fake_eval)
    monkeypatch.setattr(web, 'run_actions', fake_run_actions)
    with TestClient(web.app) as client:
        token = client.get('/api/scenes').json()['token']
        response = client.post(endpoint, headers={'X-Check-Token': token})
        assert response.status_code == 202
        run_id = response.json()['id']
        assert response.json()['scene_ids'] == [id for id, s in c.scenes.items() if s.name in names]
        for _ in range(200):
            record = client.get('/api/checks/' + run_id).json()
            if record['state'] == 'finished':
                break
            time.sleep(.01)
        else:
            pytest.fail('Run did not finish')
        assert [scene['name'] for scene in record['scenes']] == names
        assert len(record['actions']) == 1
        assert record['report_source'] == 'model'
        assert record['plain_english'] == 'Checked: ' + ', '.join(names)
        assert record['status'] == ('needs_attention' if 'Chicky' in names else 'normal')
    assert evaluated == names
    assert writes == [names]
    assert notifications == ['Checked: ' + ', '.join(names)]


def test_batch_schedule_targets_active():
    c = config()
    due = due_schedules(c, datetime.fromisoformat('2026-10-16T21:15:00-04:00'))
    assert len(due) == 1
    assert due[0][3] is None  # resolved by CheckService.submit at run start


def test_run_schema_rejects_scene_actions_and_parent_links():
    raw = config().model_dump()
    raw['scenes']['girls']['on'] = {'normal': ['notify']}
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)
    raw = config().model_dump()
    raw['scenes']['girls']['scenes'] = ['chicky']
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)
