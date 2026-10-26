import asyncio
import errno
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from settings import Configuration
import web


def config():
    return Configuration.model_validate({
        'sensors': {'cam': {'name': 'Camera'}},
        'scenes': {'coop': {'name': 'Coop', 'sensors': {'cam': {}}, 'conditions': ['Birds visible']}},
    })


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(web, 'load_config', config)
    async def no_plans(c): return c
    monkeypatch.setattr(web, 'resolve_plans', no_plans)
    monkeypatch.setattr(web, 'resolve_action_plans', no_plans)
    with TestClient(web.app, raise_server_exceptions=False) as client:
        yield client


def test_storage_failure_returns_json_and_does_not_reserve_check(client, monkeypatch, caplog):
    original = Path.write_text
    def fail_history(path, *args, **kwargs):
        if path.parent.name == 'checks':
            raise PermissionError(errno.EACCES, 'SECRET mount details')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', fail_history)
    response = client.post('/api/scenes/coop/checks')
    assert response.status_code == 500
    assert 'POST /api/scenes/coop/checks' in response.json()['detail']
    assert 'Check container logs' in response.json()['detail']
    assert 'Check storage write failed' in caplog.text
    assert 'PermissionError' in caplog.text
    assert 'Traceback (most recent call last)' in caplog.text
    assert 'SECRET' not in response.text
    assert client.get('/api/scenes').json()['active_check'] is None


def test_bad_history_returns_json_error_instead_of_plain_text(client, caplog):
    (web.app.state.folder / ('a' * 32 + '.json')).write_text('SECRET invalid JSON')
    response = client.get('/api/checks')
    assert response.status_code == 500
    assert 'GET /api/checks' in response.json()['detail']
    assert 'JSONDecodeError' in caplog.text
    assert 'SECRET' not in response.text
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('kind', ['scene', 'manual_action'])
def test_final_save_failure_always_releases_busy_guard(monkeypatch, kind):
    async def evaluate(*args): return {'status': 'normal', 'summary': 'All good'}
    async def no_actions(*args): pass
    async def execute(*args): return {'status': 'succeeded'}
    def fail_save(record): raise OSError(errno.ENOSPC, 'Disk full')
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'run_actions', no_actions)
    monkeypatch.setattr(web, 'execute', execute)
    monkeypatch.setattr(web, 'save', fail_save)
    web.app.state.config = config()
    web.app.state.active = 'running'
    record = {'id': 'running', 'state': 'running'}
    task = (web.run_check(record, config().scenes['coop']) if kind == 'scene'
            else web.run_manual(record, 'off'))
    with pytest.raises(OSError): asyncio.run(task)
    assert web.app.state.active is None
