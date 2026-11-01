import asyncio
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import httpx
import pytest

from mcp_servers.yolink_server import YoLinkClient, read_door
from settings import Configuration, ROOT, Sensor
from sensors import normalize
from mcp.types import CallToolResult, TextContent
import mcp_servers.yolink_server as yolink


def run_read(monkeypatch, *, state='closed', online=True, report=None, code='000000', device_id='door-id'):
    monkeypatch.setenv('YOLINK_UAID', 'test-id')
    monkeypatch.setenv('YOLINK_SECRET_KEY', 'test-secret')
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/token'):
            assert b'client_credentials' in request.content
            return httpx.Response(200, json={'access_token': 'access', 'expires_in': 3600})
        assert request.headers['Authorization'] == 'Bearer access'
        payload = json.loads(request.content)
        if payload['method'] == 'Home.getDeviceList':
            data = {'devices': [{'deviceId': 'door-id', 'type': 'DoorSensor', 'token': 'device-secret'}]}
        else:
            assert payload['targetDevice'] == 'door-id'
            assert payload['token'] == 'device-secret'
            data = {'deviceId': device_id, 'online': online, 'reportAt': report,
                    'state': {'state': state, 'battery': 3, 'stateChangedAt': '2020-01-01T00:00:00Z'}}
        return httpx.Response(200, json={'code': code, 'data': data})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await YoLinkClient(http).read('door', 'door-id')
    return asyncio.run(run()), requests


@pytest.mark.parametrize('state', ['open', 'closed'])
def test_read_door(monkeypatch, tmp_path, state):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    stamp = datetime.now(timezone.utc).isoformat()
    envelope, requests = run_read(monkeypatch, state=state, report=stamp)
    assert len(requests) == 3
    assert envelope['reading']['state'] == state
    assert envelope['observed_at'] == stamp
    assert 'secret' not in json.dumps(envelope)
    result = CallToolResult(content=[TextContent(type='text', text=json.dumps(envelope))])
    metadata, _ = normalize(result, 'door', Sensor(name='Door'))
    assert metadata['reading']['battery'] == 3


@pytest.mark.parametrize('kwargs', [
    {'online': False}, {'state': 'error'}, {'report': None},
    {'report': 'bad-date'}, {'report': '2026-01-01T00:00:00'},
    {'code': '000201'}, {'device_id': 'wrong'},
])
def test_invalid_reading(monkeypatch, kwargs):
    with pytest.raises(ValueError):
        run_read(monkeypatch, **kwargs)


def test_old_report_accepted_without_retimestamping(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    envelope, _ = run_read(monkeypatch, report='2020-01-01T00:00:00Z')
    result = CallToolResult(content=[TextContent(type='text', text=json.dumps(envelope))])
    metadata, _ = normalize(result, 'door', Sensor(name='Door'))
    assert datetime.fromisoformat(metadata['observed_at']) == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_online_cached_state_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    report = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    envelope, _ = run_read(monkeypatch, report=report)
    result = CallToolResult(content=[TextContent(type='text', text=json.dumps(envelope))])
    metadata, _ = normalize(result, 'door', Sensor(name='Door'))
    assert metadata['reading']['state'] == 'closed'
    assert metadata['reading']['online'] is True
    assert metadata['observed_at'] == report
    assert metadata['reading']['stateChangedAt'] == '2020-01-01T00:00:00Z'


def test_millisecond_report(monkeypatch):
    envelope, _ = run_read(monkeypatch, report=1700000000000)
    assert datetime.fromisoformat(envelope['observed_at']).timestamp() == 1700000000


def test_auth_retry_and_expiry(monkeypatch):
    monkeypatch.setenv('YOLINK_UAID', 'id')
    monkeypatch.setenv('YOLINK_SECRET_KEY', 'secret')
    tokens, calls = [], []
    def handler(request):
        if request.url.path.endswith('/token'):
            tokens.append(True)
            return httpx.Response(200, json={'access_token': str(len(tokens)), 'expires_in': 3600})
        calls.append(True)
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={'code': '000000', 'data': {'devices': []}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = YoLinkClient(http)
            assert await client.devices() == []
            assert await client.devices() == []
            assert len(tokens) == 2
            client.expires_at = 0
            await client.devices()
            assert len(tokens) == 3
    asyncio.run(run())


def test_credentials_required(monkeypatch):
    monkeypatch.delenv('YOLINK_UAID', raising=False)
    monkeypatch.delenv('YOLINK_SECRET_KEY', raising=False)
    async def run():
        async with httpx.AsyncClient() as http:
            with pytest.raises(ValueError, match='Set YOLINK'):
                await YoLinkClient(http).devices()
    asyncio.run(run())


def test_assignment_guard(monkeypatch):
    monkeypatch.setenv('SCENE_SENSOR_IDS', '[]')
    with pytest.raises(ValueError, match='not assigned'):
        asyncio.run(read_door('unassigned', 'other-device'))


def test_bundled_door_configuration(monkeypatch):
    from settings import load_config
    monkeypatch.setenv('SCENES_FILE', str(ROOT / 'scenes.yaml'))
    monkeypatch.setenv('SENSORS_FILE', str(ROOT / 'sensors.yaml'))
    config = load_config()
    doors = [sensor for sensor in config.sensors.values() if sensor.tool == 'read_door']
    assert doors
    assert all(config.servers[sensor.server].args == ['-m', 'mcp_servers.yolink_server'] for sensor in doors)


@pytest.mark.parametrize('failure,stage,reason', [
    ('credentials', 'authentication', 'Set YOLINK_UAID and YOLINK_SECRET_KEY'),
    ('authentication', 'authentication', 'HTTP 401'),
    ('offline', 'door state validation', 'offline'),
    ('timestamp', 'report timestamp validation', 'Invalid YoLink response or report timestamp'),
    ('transport', 'authentication', 'cloud connection failed'),
    ('rejected', 'Home.getDeviceList', 'code=000201'),
    ('bad_code', 'Home.getDeviceList', 'code=invalid'),
])
def test_adapter_logs_specific_failures_without_credentials(monkeypatch, caplog, failure, stage, reason):
    monkeypatch.setenv('SCENE_SENSOR_IDS', '["girls_door"]')
    monkeypatch.setenv('YOLINK_UAID', 'SECRET-client')
    monkeypatch.setenv('YOLINK_SECRET_KEY', 'SECRET-password')
    if failure == 'credentials':
        monkeypatch.delenv('YOLINK_SECRET_KEY')
    monkeypatch.setattr(yolink, 'load_config', lambda: SimpleNamespace(sensors={
        'girls_door': SimpleNamespace(arguments={'device_id': 'SECRET-device'})}))
    def handler(request):
        if failure == 'transport':
            raise httpx.ConnectError('SECRET-connection-url', request=request)
        if request.url.path.endswith('/token'):
            return httpx.Response(401 if failure == 'authentication' else 200,
                                  json={'access_token': 'SECRET-token', 'expires_in': 3600})
        if failure in ('rejected', 'bad_code'):
            return httpx.Response(200, json={'code': '000201' if failure == 'rejected' else 'SECRET-error',
                                            'data': {}})
        payload = json.loads(request.content)
        data = ({'devices': [{'deviceId': 'SECRET-device', 'type': 'DoorSensor', 'token': 'SECRET-device-token'}]}
                if payload['method'] == 'Home.getDeviceList' else
                {'deviceId': 'SECRET-device', 'online': failure != 'offline',
                 'state': {'state': 'closed'}, 'reportAt': 'SECRET-invalid-timestamp'})
        return httpx.Response(200, json={'code': '000000', 'data': data})
    original_client = httpx.AsyncClient
    monkeypatch.setattr(yolink.httpx, 'AsyncClient', lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(ValueError, match='reading unavailable'):
        asyncio.run(yolink.read_door('girls_door', 'SECRET-device'))
    assert 'YoLink read failed sensor=girls_door' in caplog.text
    assert f'stage={stage}' in caplog.text
    assert reason in caplog.text
    assert 'SECRET-' not in caplog.text
