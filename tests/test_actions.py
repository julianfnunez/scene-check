import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from actions import context_for, execute, render, run_actions, selected_actions
from settings import Configuration, ConfigLoader
import yaml


def config():
    return Configuration.model_validate({
        'servers': {'alerts': {'args': ['-m', 'mcp_servers.alert_server']}},
        'sensors': {},
        'actions': {
            'red': {'name': 'Red', 'server': 'alerts', 'tool': 'set_light', 'arguments': {'on': True}},
            'notify': {'name': 'Notify', 'server': 'alerts', 'tool': 'publish',
                       'arguments': {'message': '{{ result.summary }}'}},
            'off': {'name': 'Off', 'server': 'alerts', 'tool': 'set_light',
                    'manual': True, 'arguments': {'on': False}},
        },
        'scenes': {'coop': {'name': 'Coop', 'scenes': ['child'], 'conditions': ['Child passes'],
                            'on': {'not_normal': ['red', 'notify'], 'unknown': ['notify']}},
                   'child': {'name': 'Child', 'scenes': ['leaf'], 'conditions': ['Leaf passes']},
                   'leaf': {'name': 'Leaf', 'sensors': {'cam': {}}, 'conditions': ['Birds visible']}},
    } | {'sensors': {'cam': {'name': 'Camera', 'server': 'alerts'}}})


@pytest.mark.parametrize('status,expected', [('normal', []), ('unknown', ['notify', 'red']),
                                            ('needs_attention', ['red', 'notify'])])
def test_triggers(status, expected):
    assert selected_actions(config().scenes['coop'], status) == expected


def test_optional_success_notifications():
    scene = config().scenes['coop']
    scene.on.normal = ['notify']
    assert selected_actions(scene, 'normal') == ['notify']


@pytest.mark.parametrize('status,override,expected', [
    ('normal', {}, 3), ('needs_attention', {}, 4), ('unknown', {}, 4),
    ('normal', {'priority': 5}, 5), ('needs_attention', {'priority': 2}, 2),
])
def test_status_defaults_and_explicit_override(status, override, expected):
    from settings import ActionInstructions, ActionStep
    c = config()
    c.actions['notify'].arguments = {'message': 'Check finished', 'priority': 4}
    c.actions['notify'].arguments_by_status = {'normal': {'priority': 3}}
    rule = ActionInstructions(instructions='Notify', actions=['notify'])
    rule._steps = [ActionStep(action_id='notify', arguments=override)]
    scene = c.scenes['coop']
    scene.on.normal = []
    scene.on.not_normal = []
    scene.on.unknown = []
    setattr(scene.on, status, rule)
    observed = []
    async def executor(action_id, effective, context):
        observed.append(effective.actions[action_id].arguments['priority'])
        return {'status': 'succeeded'}
    asyncio.run(run_actions(record() | {'status': status}, scene, c, lambda r: None, executor))
    assert observed == [expected]
    assert c.actions['notify'].arguments['priority'] == 4


def test_templates_are_data():
    context = {'result': {'summary': '{{ scene.secret }}', 'conditions': [{'status': 'unknown'}]}}
    assert render('Text {{ result.summary }}', context) == 'Text {{ scene.secret }}'
    assert render({'conditions': '{{ result.conditions }}', 'on': False}, context) == {
        'conditions': [{'status': 'unknown'}], 'on': False}
    with pytest.raises(ValueError):
        render('{{ result.missing }}', context)


def test_yaml_on_and_booleans():
    assert yaml.load('on: {not_normal: [red]}\narguments: {on: false}', Loader=ConfigLoader) == {
        'on': {'not_normal': ['red']}, 'arguments': {'on': False}}


@pytest.mark.parametrize('change', ['server', 'action', 'trigger'])
def test_bad_references(change):
    raw = config().model_dump()
    if change == 'server':
        raw['actions']['red']['server'] = 'missing'
    elif change == 'action':
        raw['scenes']['coop']['on']['normal'] = ['missing']
    else:
        raw['scenes']['coop']['on']['typo'] = ['red']
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)


def record():
    return {'id': 'a' * 32, 'scene_id': 'coop', 'scene_name': 'Coop', 'created_at': 'now',
            'status': 'needs_attention', 'summary': 'Door open', 'conditions': [],
            'children': [{'name': 'Girls', 'status': 'unknown', 'conditions': [
                {'condition': 'Three chickens', 'status': 'unknown', 'explanation': 'Dark'}]}]}


def test_independent_actions_and_persisted_intent():
    rec = record()
    snapshots, calls = [], []
    async def executor(action_id, configuration, context):
        assert snapshots[-1]['actions'][-1]['status'] == 'uncertain'
        assert 'Girls' in context['result']['details']
        calls.append(action_id)
        return {'action_id': action_id, 'status': 'failed' if action_id == 'red' else 'succeeded'}
    asyncio.run(run_actions(rec, config().scenes['coop'], config(),
                           lambda r: snapshots.append(json.loads(json.dumps(r))), executor))
    assert calls == ['red', 'notify']
    assert [a['status'] for a in rec['actions']] == ['failed', 'succeeded']
    assert rec['status'] == 'needs_attention'


def test_cancel_during_action_preserves_intent_and_releases_busy(monkeypatch):
    import web
    rec = record() | {'state': 'running'}
    snapshots = []
    async def evaluate(*args): return {'status': 'needs_attention', 'summary': 'Door open'}
    async def cancel(action_id, configuration, context):
        raise asyncio.CancelledError()
    async def run(rec, scene, configuration, save):
        return await run_actions(rec, scene, configuration, save, executor=cancel)
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'run_actions', run)
    monkeypatch.setattr(web, 'save', lambda r: snapshots.append(json.loads(json.dumps(r))))
    monkeypatch.setattr(web, 'check_timeout', lambda *args: 10)
    web.app.state.config = config()
    web.app.state.active = rec['id']
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(web.run_check(rec, config().scenes['coop']))
    assert rec['state'] == 'finished'
    assert rec['status'] == 'needs_attention'
    assert rec['actions'][0]['status'] == 'uncertain'
    assert web.app.state.active is None


@pytest.mark.parametrize('mode', ['success', 'error', 'missing', 'timeout'])
def test_real_mcp_action(tmp_path, mode, caplog):
    server = tmp_path / 'adapter.py'
    log = tmp_path / 'calls.jsonl'
    server.write_text('''import asyncio, json, sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP
mcp = FastMCP('test')
@mcp.tool()
async def act(value: str):
    with Path(sys.argv[1]).open('a') as f: f.write(json.dumps(value)+'\\n')
    if sys.argv[2] == 'error': raise ValueError('SECRET vendor response')
    if sys.argv[2] == 'timeout': await asyncio.sleep(30)
    return {'done': True}
mcp.run(transport='stdio')
''')
    raw = config().model_dump()
    raw['servers']['alerts'].update(args=[str(server), str(log), mode], timeout_seconds=3 if mode == 'timeout' else 10)
    raw['actions']['red'].update(tool='missing' if mode == 'missing' else 'act', arguments={'value': '{{ result.summary }}'})
    result = asyncio.run(execute('red', Configuration.model_validate(raw), context_for(record())))
    assert result['status'] == ('succeeded' if mode == 'success' else 'failed')
    assert 'SECRET' not in json.dumps(result)
    if mode != 'success':
        assert result['error_stage']
        assert 'Action red failed at' in caplog.text
        assert 'SECRET' not in caplog.text
    assert (log.read_text().splitlines() if log.exists() else []) == ([] if mode == 'missing' else ['"Door open"'])


def test_web_actions_and_restart(tmp_path, monkeypatch):
    import web
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(web, 'load_config', config)
    async def plans(c): return c
    monkeypatch.setattr(web, 'resolve_plans', plans)
    calls = []
    async def evaluate(*args): return {'status': 'unknown', 'summary': 'Check failed', 'conditions': []}
    async def action(action_id, configuration, context):
        calls.append(action_id)
        return {'action_id': action_id, 'name': action_id, 'status': 'succeeded'}
    async def run(rec, scene, configuration, save):
        return await run_actions(rec, scene, configuration, save, executor=action)
    monkeypatch.setattr(web, 'evaluate', evaluate)
    monkeypatch.setattr(web, 'run_actions', run)
    monkeypatch.setattr(web, 'execute', action)
    with TestClient(web.app) as client:
        headers = {'X-Check-Token': client.get('/api/scenes').json()['token']}
        assert client.post('/api/scenes/coop/checks').status_code == 403
        assert client.post('/api/actions/off/runs').status_code == 403
        assert client.post('/api/actions/red/runs', headers=headers).status_code == 404
        def wait(response):
            assert response.status_code == 202
            for _ in range(200):
                rec = client.get('/api/checks/' + response.json()['id']).json()
                if rec['state'] == 'finished': return rec
                time.sleep(.01)
            pytest.fail('Job did not finish')
        rec = wait(client.post('/api/scenes/coop/checks', headers=headers))
        assert rec['status'] == 'unknown'
        assert calls == ['notify', 'red']
        rec = wait(client.post('/api/actions/off/runs', headers=headers))
        assert rec['actions'][0]['action_id'] == 'off'
        assert client.get('/api/scenes').headers['Cache-Control'] == 'no-store'
    rec['state'] = 'running'
    rec['actions'][0]['status'] = 'uncertain'
    (tmp_path / 'checks' / (rec['id'] + '.json')).write_text(json.dumps(rec))
    calls.clear()
    with TestClient(web.app) as client:
        restored = client.get('/api/checks/' + rec['id']).json()
        assert restored['state'] == 'finished'
        assert restored['actions'][0]['status'] == 'uncertain'
        assert calls == []


def test_alert_adapters(monkeypatch):
    from mcp_servers import alert_server
    states, requests = [], []
    monkeypatch.setattr(alert_server, 'hue_set_state', states.append)
    alert_server.set_light(True, alert='lselect')
    alert_server.set_light(False)
    assert states == [{'on': True, 'alert': 'lselect', 'hue': 0, 'sat': 254, 'bri': 254},
                      {'on': False, 'alert': 'none'}]
    monkeypatch.setenv('NTFY_URL', 'https://ntfy.example/topic')
    monkeypatch.setenv('NTFY_TOKEN', 'secret')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def urlopen(req, **kwargs):
        requests.append(req)
        return Response()
    monkeypatch.setattr(alert_server.urllib.request, 'urlopen', urlopen)
    assert alert_server.publish('Door open', title='Coop — alert') == {'accepted': True}
    assert requests[0].data == b'Door open'
    assert requests[0].get_header('Authorization') == 'Bearer secret'


@pytest.mark.parametrize('kind,expected', [
    ('unauthorized', 'HTTP 401'), ('forbidden', 'HTTP 403'),
    ('rate_limit', 'HTTP 429'), ('dns', 'DNS lookup failed'),
    ('tls', 'TLS certificate/connection error'), ('timeout', 'TimeoutError'),
])
def test_notification_failure_diagnostics_redact_sensitive_data(monkeypatch, caplog, kind, expected):
    import socket
    import ssl
    import urllib.error
    from mcp_servers import alert_server
    monkeypatch.setenv('NTFY_URL', 'https://SECRET.example/SECRET-topic')
    monkeypatch.setenv('NTFY_TOKEN', 'SECRET-token')
    failures = {
        'unauthorized': urllib.error.HTTPError('https://SECRET', 401, 'SECRET', {}, None),
        'forbidden': urllib.error.HTTPError('https://SECRET', 403, 'SECRET', {}, None),
        'rate_limit': urllib.error.HTTPError('https://SECRET', 429, 'SECRET', {}, None),
        'dns': urllib.error.URLError(socket.gaierror('SECRET-host')),
        'tls': urllib.error.URLError(ssl.SSLError('SECRET-host')),
        'timeout': TimeoutError('SECRET-request'),
    }
    def fail(*args, **kwargs): raise failures[kind]
    monkeypatch.setattr(alert_server.urllib.request, 'urlopen', fail)
    with pytest.raises(RuntimeError) as exc:
        alert_server.publish('SECRET-message')
    assert expected in caplog.text
    assert 'SECRET' not in caplog.text + str(exc.value)


def test_missing_notification_url_logs_without_sending(monkeypatch, caplog):
    from mcp_servers import alert_server
    monkeypatch.delenv('NTFY_URL', raising=False)
    def fail(*args, **kwargs): pytest.fail('Missing URL must not send anything')
    monkeypatch.setattr(alert_server.urllib.request, 'urlopen', fail)
    with pytest.raises(ValueError): alert_server.publish('SECRET-message')
    assert 'ntfy configuration invalid' in caplog.text
    assert 'SECRET' not in caplog.text
