"""Generic MCP observation collection. No device-specific calls or parsing."""
import asyncio
import base64
import json
import logging
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from settings import ROOT, data_dir

logger = logging.getLogger(__name__)


class ObservationError(ValueError):
    """A host-generated diagnostic, safe to log without vendor data."""


def failure_reason(exc):
    if isinstance(exc, BaseExceptionGroup):
        return '; '.join(dict.fromkeys(failure_reason(child) for child in exc.exceptions[:3]))
    if isinstance(exc, ObservationError):
        return str(exc)
    if isinstance(exc, TimeoutError):
        return 'Timed out waiting for the MCP server or tool'
    if isinstance(exc, FileNotFoundError):
        return 'Executable or required file not found'
    if isinstance(exc, PermissionError):
        return 'Permission denied accessing the executable or observation storage'
    if isinstance(exc, OSError):
        return f'Operating system error (errno={exc.errno})'
    # Exception text, tool error bodies, and nested exception groups can contain secrets.
    return f'{type(exc).__name__}; check adapter logs for details'


def normalize(result, sensor_id, sensor):
    if result.isError:
        raise ObservationError('MCP tool reported an error; check adapter logs for details')
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
        raise ObservationError('Sensor must return an observation envelope with a timestamp')
    envelope = envelopes[0]
    if envelope.get('sensor_id') != sensor_id:
        raise ObservationError('Observation sensor ID does not match requested sensor')
    stamp = envelope.get('observed_at', envelope.get('captured_at'))
    try:
        observed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        raise ObservationError('Observation timestamp is missing or invalid') from None
    if observed.tzinfo is None:
        raise ObservationError('Observation timestamp needs a timezone')
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if age < -5:
        raise ObservationError(f'Observation timestamp is {-age:.0f}s in the future (allowed=5s)')
    reading = envelope.get('reading')
    if (reading is None or reading == {} or reading == [] or reading == '') and not images:
        raise ObservationError('Observation must contain a reading or image')
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
            raise ObservationError('Unsupported image type')
        try:
            raw = base64.b64decode(image.data, validate=True)
        except ValueError:
            raise ObservationError('Invalid image encoding') from None
        if not raw or len(raw) > 15_000_000:
            raise ObservationError('Invalid image size')
        extension = {'image/jpeg':'jpg','image/png':'png','image/webp':'webp'}[image.mimeType]
        filename = uuid4().hex + '.' + extension
        (folder / filename).write_bytes(raw)
        metadata['images'].append(filename)
        content.append({'type':'input_image','detail':'high','image_url':f'data:{image.mimeType};base64,{image.data}'})
    return metadata, [{'type':'input_text','text':json.dumps(metadata)}, *content]


async def collect_series(sensor_id, sampling, config, progress, trace, evidence, *, scene_name=None):
    sensor = config.sensors[sensor_id]
    server = config.servers[sensor.server]
    env = {**os.environ, **server.env, 'SCENE_SENSOR_IDS':json.dumps([sensor_id])}
    command = sys.executable if server.command == 'python' else server.command
    params = StdioServerParameters(command=command,args=server.args,env=env,cwd=str(ROOT))
    observations = []
    stage = 'MCP server startup'
    loop = asyncio.get_running_loop()
    logger.info('Collecting observations scene=%s sensor=%s server=%s tool=%s count=%s interval=%gs',
                scene_name or '-', sensor_id, sensor.server, sensor.tool,
                sampling.evaluations, sampling.interval_seconds)

    def failed(exc, index=None):
        reason = failure_reason(exc)
        logger.error('Observation failed scene=%s sensor=%s server=%s tool=%s observation=%s/%s stage=%s reason=%s',
                     scene_name or '-', sensor_id, sensor.server, sensor.tool,
                     index + 1 if index is not None else '-', sampling.evaluations, stage, reason)
        event = {'sensor_id': sensor_id, 'server': sensor.server, 'tool': sensor.tool,
                 'status': 'failed', 'stage': stage, 'error': reason,
                 'recorded_at': datetime.now(timezone.utc).isoformat()}
        if index is not None:
            event['evaluation'] = index + 1
        trace.append(event)

    try:
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                async with asyncio.timeout(server.timeout_seconds):
                    stage = 'MCP initialization'
                    await session.initialize()
                    stage = 'MCP tool discovery'
                    listing = await session.list_tools()
                    if sensor.tool not in {t.name for t in listing.tools}:
                        raise ObservationError('Configured MCP tool was not found')
                previous_start = None
                for index in range(sampling.evaluations):
                    if previous_start is not None:
                        await asyncio.sleep(max(0,previous_start+sampling.interval_seconds-loop.time()))
                    previous_start = loop.time()
                    progress(f'Observing {sensor.name} {index+1}/{sampling.evaluations}',trace,evidence)
                    try:
                        stage = 'MCP tool invocation'
                        async with asyncio.timeout(server.timeout_seconds):
                            result = await session.call_tool(sensor.tool, {**sensor.arguments, 'sensor_id':sensor_id})
                        if result.isError:
                            raise ObservationError('MCP tool reported an error; check adapter logs for details')
                        stage = 'observation validation and storage'
                        observations.append(normalize(result,sensor_id,sensor))
                        logger.info('Observation captured scene=%s sensor=%s observation=%s/%s elapsed=%.1fs',
                                    scene_name or '-', sensor_id, index + 1, sampling.evaluations,
                                    loop.time() - previous_start)
                    except Exception as exc:
                        failed(exc, index)
                        observations.append(None)
                stage = 'MCP session cleanup'
    except Exception as exc:
        failed(exc)
        # Preserve successful readings and fill all missing evaluations as unknown.
        observations.extend([None] * (sampling.evaluations-len(observations)))
    return observations
