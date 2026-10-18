"""Generic MCP observation collection. No device-specific calls or parsing."""
import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from settings import ROOT, data_dir


def normalize(result, sensor_id, sensor):
    if result.isError:
        raise ValueError('Sensor tool reported an error')
    structured = getattr(result, 'structuredContent', None)
    envelopes = [structured] if isinstance(structured, dict) and 'observed_at' in structured else []
    images = []
    for block in result.content:
        if block.type == 'text':
            try:
                value = json.loads(block.text)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and ('observed_at' in value or 'captured_at' in value):
                envelopes.append(value)
        elif block.type == 'image':
            images.append(block)
    if not envelopes:
        raise ValueError('Sensor must return an observation envelope with a timestamp')
    envelope = envelopes[0]
    if envelope.get('sensor_id') != sensor_id:
        raise ValueError('Observation sensor ID does not match requested sensor')
    stamp = envelope.get('observed_at', envelope.get('captured_at'))
    observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if observed.tzinfo is None:
        raise ValueError('Observation timestamp needs a timezone')
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if age < -5 or age > sensor.max_age_seconds:
        raise ValueError('Observation timestamp is stale or in the future')
    reading = envelope.get('reading')
    if (reading is None or reading == {} or reading == [] or reading == '') and not images:
        raise ValueError('Observation must contain a reading or image')
    evidence_id = uuid4().hex
    metadata = {'sensor_id':sensor_id, 'evidence_id':evidence_id,
                'observed_at':observed.isoformat(), 'captured_at':observed.isoformat(),
                'received_at':datetime.now(timezone.utc).isoformat(),
                'reading':reading, 'images':[], 'tool':sensor.tool, 'server':sensor.server}
    content = []
    folder = data_dir() / 'frames'
    folder.mkdir(exist_ok=True)
    for image in images:
        if image.mimeType not in ('image/jpeg', 'image/png', 'image/webp'):
            raise ValueError('Unsupported image type')
        raw = base64.b64decode(image.data, validate=True)
        if not raw or len(raw) > 15_000_000:
            raise ValueError('Invalid image size')
        extension = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[image.mimeType]
        filename = uuid4().hex + '.' + extension
        (folder / filename).write_bytes(raw)
        metadata['images'].append(filename)
        content.append({'type':'input_image','detail':'high','image_url':f'data:{image.mimeType};base64,{image.data}'})
    return metadata, [{'type':'input_text','text':json.dumps(metadata)}, *content]


async def collect_series(sensor_id, sampling, config, progress, trace, evidence):
    sensor = config.sensors[sensor_id]
    server = config.servers[sensor.server]
    env = {**os.environ, **server.env, 'SCENE_SENSOR_IDS':json.dumps([sensor_id])}
    command = sys.executable if server.command == 'python' else server.command
    params = StdioServerParameters(command=command,args=server.args,env=env,cwd=str(ROOT))
    observations = []
    try:
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                async with asyncio.timeout(server.timeout_seconds):
                    await session.initialize()
                    listing = await session.list_tools()
                    if sensor.tool not in {t.name for t in listing.tools}:
                        raise ValueError('Configured MCP tool was not found')
                previous_start = None
                loop = asyncio.get_running_loop()
                for index in range(sampling.evaluations):
                    if previous_start is not None:
                        await asyncio.sleep(max(0,previous_start+sampling.interval_seconds-loop.time()))
                    previous_start = loop.time()
                    progress(f'Observing {sensor.name} {index+1}/{sampling.evaluations}',trace,evidence)
                    try:
                        async with asyncio.timeout(server.timeout_seconds):
                            result = await session.call_tool(sensor.tool, {**sensor.arguments, 'sensor_id':sensor_id})
                        observations.append(normalize(result,sensor_id,sensor))
                    except Exception:
                        observations.append(None)
    except Exception:
        # Preserve successful readings and fill all missing evaluations as unknown.
        observations.extend([None] * (sampling.evaluations-len(observations)))
    return observations
