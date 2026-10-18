"""Read-only YoLink cloud MCP adapter. Credentials never enter observations."""
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP
from settings import load_config

mcp = FastMCP('YoLink doors')
BASE = 'https://api.yosmart.com/open/yolink'


class YoLinkClient:
    def __init__(self, client):
        self.client = client
        self.access_token = None
        self.expires_at = 0

    async def authenticate(self):
        uaid = os.environ.get('YOLINK_UAID')
        secret = os.environ.get('YOLINK_SECRET_KEY')
        if not uaid or not secret:
            raise ValueError('Set YOLINK_UAID and YOLINK_SECRET_KEY')
        response = await self.client.post(BASE + '/token', data={
            'grant_type': 'client_credentials', 'client_id': uaid, 'client_secret': secret})
        if response.status_code != 200:
            raise ValueError('YoLink authentication failed')
        data = response.json()
        token = data.get('access_token')
        lifetime = float(data.get('expires_in', 0))
        if not isinstance(token, str) or not token or lifetime <= 0:
            raise ValueError('Invalid YoLink authentication response')
        self.access_token = token
        self.expires_at = time.monotonic() + max(0, lifetime - 30)

    async def call(self, method, **arguments):
        for attempt in range(2):
            if not self.access_token or time.monotonic() >= self.expires_at:
                await self.authenticate()
            response = await self.client.post(BASE + '/v2/api',
                headers={'Authorization': 'Bearer ' + self.access_token},
                json={'method': method, 'time': int(time.time() * 1000), **arguments})
            if response.status_code == 401 and attempt == 0:
                self.access_token = None
                continue
            if response.status_code != 200:
                raise ValueError('YoLink API request failed')
            result = response.json()
            if result.get('code') != '000000' or not isinstance(result.get('data'), dict):
                raise ValueError('YoLink API rejected the request or returned invalid data')
            return result['data']

    async def devices(self):
        devices = (await self.call('Home.getDeviceList')).get('devices')
        if not isinstance(devices, list):
            raise ValueError('Invalid YoLink device list')
        return devices

    async def read(self, sensor_id, device_id):
        device = next((d for d in await self.devices()
                       if d.get('deviceId') == device_id and d.get('type') == 'DoorSensor'), None)
        if not device or not isinstance(device.get('token'), str) or not device['token']:
            raise ValueError('Configured YoLink door sensor was not found')
        data = await self.call('DoorSensor.getState', targetDevice=device_id, token=device['token'])
        if data.get('deviceId') != device_id or data.get('online') is not True:
            raise ValueError('YoLink door sensor is offline or returned a different device')
        state = data.get('state')
        if not isinstance(state, dict) or state.get('state') not in ('open', 'closed'):
            raise ValueError('YoLink door sensor returned an invalid state')
        # reportAt is the observation time; stateChangedAt can legitimately be days old.
        stamp = data.get('reportAt')
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            observed = datetime.fromtimestamp(stamp / 1000, timezone.utc)
        elif isinstance(stamp, str):
            observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        else:
            raise ValueError('YoLink door reading has no report timestamp')
        if observed.tzinfo is None:
            raise ValueError('YoLink report timestamp needs a timezone')
        reading = {'state': state['state'], 'online': True}
        for key in ('battery', 'stateChangedAt'):
            if key in state:
                reading[key] = state[key]
        return {'sensor_id': sensor_id, 'observed_at': observed.isoformat(), 'reading': reading}


@mcp.tool()
async def read_door(sensor_id: str, device_id: str) -> str:
    """Read the assigned YoLink contact sensor's latest cloud-reported door state."""
    allowed = json.loads(os.environ.get('SCENE_SENSOR_IDS', '[]'))
    config = load_config()
    if sensor_id not in allowed or sensor_id not in config.sensors:
        raise ValueError('Sensor is not assigned to this scene')
    if config.sensors[sensor_id].arguments.get('device_id') != device_id:
        raise ValueError('Device does not match the configured sensor')
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            return json.dumps(await YoLinkClient(client).read(sensor_id, device_id))
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        # Do not expose cloud response bodies, URLs, or credentials through MCP errors.
        raise ValueError('YoLink reading unavailable; check credentials, device, connectivity and report timestamp') from None


async def list_devices():
    async with httpx.AsyncClient(timeout=10) as client:
        for device in await YoLinkClient(client).devices():
            if device.get('type') == 'DoorSensor':
                print(json.dumps({key: device.get(key) for key in ('deviceId', 'name', 'type')}))


if __name__ == '__main__':
    if sys.argv[1:] == ['--list-devices']:
        try:
            asyncio.run(list_devices())
        except Exception:
            sys.exit('Unable to list YoLink doors; check credentials and connectivity')
    else:
        mcp.run(transport='stdio')
