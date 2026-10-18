"""Local MCP server. Only captures images; it contains no scene evaluation logic."""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from mcp.server.fastmcp import FastMCP, Image
from settings import load_config, data_dir

mcp = FastMCP('Scene Watch cameras')

async def capture(sensor_id: str, rtsp_env: str | None = None):
    config = load_config()
    allowed = json.loads(os.environ.get('SCENE_SENSOR_IDS', '[]'))
    if sensor_id not in allowed or sensor_id not in config.sensors:
        raise ValueError('Sensor is not assigned to this scene')
    url = os.environ.get(rtsp_env or config.sensors[sensor_id].rtsp_env or '', '')
    if not url.startswith(('rtsp://', 'rtsps://')):
        raise ValueError('Set the RTSP environment variable for this camera')
    folder = data_dir() / 'frames'
    folder.mkdir(exist_ok=True)
    path = folder / (uuid4().hex + '.jpg')
    proc = await asyncio.create_subprocess_exec(
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-rtsp_transport', 'tcp',
        '-i', url, '-frames:v', '1', '-vf', "scale=w='min(1280,iw)':h=-2",
        '-update', '1', '-y', str(path), stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(proc.wait(), 30)
        if proc.returncode or not path.exists() or not path.stat().st_size:
            raise RuntimeError('Camera capture failed; check RTSP URL and camera connectivity')
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        path.unlink(missing_ok=True)
        raise
    return path, datetime.now(timezone.utc).isoformat()

@mcp.tool()
async def capture_camera(sensor_id: str, rtsp_env: str | None = None) -> list:
    """Capture one fresh camera frame. Returns sensor ID, timestamp, evidence ID, and JPEG image.
    Use only sensor IDs assigned to the scene. A second capture may clarify an uncertain view.
    """
    path, timestamp = await capture(sensor_id, rtsp_env)
    output = [json.dumps({'sensor_id': sensor_id, 'observed_at': timestamp,
                        'evidence_id': path.name}), Image(data=path.read_bytes(), format="jpeg")]
    path.unlink(missing_ok=True)
    return output

if __name__ == '__main__':
    mcp.run(transport='stdio')
