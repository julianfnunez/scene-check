"""Optional Hue and ntfy MCP adapters; secrets stay in the adapter environment."""
import os
import urllib.error
import urllib.request
from typing import Annotated, Literal
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from .notifications import hue_set_state

mcp = FastMCP('Scene Watch alerts')

@mcp.tool()
def set_light(on: bool, hue: Annotated[int, Field(ge=0, le=65535)] = 0,
              saturation: Annotated[int, Field(ge=0, le=254)] = 254,
              brightness: Annotated[int, Field(ge=1, le=254)] = 254,
              alert: Literal['none', 'select', 'lselect'] = 'none') -> dict:
    """Set the configured Hue light. Hue: 0 red, roughly 25500 green, 46920 blue.
    Saturation 254 is vivid; 0 is white. Brightness 254 is full, around 32 is dark/dim.
    Dark green uses green hue, high saturation, low brightness. lselect pulses briefly.
    """
    if not 0 <= hue <= 65535 or not 0 <= saturation <= 254 or not 1 <= brightness <= 254 or alert not in ('none', 'select', 'lselect'):
        raise ValueError('Invalid light settings')
    payload = {'on': on, 'alert': alert}
    if on:
        payload.update(hue=hue, sat=saturation, bri=brightness)
    hue_set_state(payload)
    return {'confirmed': True}

@mcp.tool()
def publish(message: str, title: str = 'Scene Watch', priority: Annotated[int, Field(ge=1, le=5)] = 4,
            topic_env: str = 'NTFY_URL') -> dict:
    """Send a notification to a topic URL configured in an environment variable."""
    url = os.environ.get(topic_env, '').strip()
    if not url.startswith(('https://', 'http://')) or not message.strip() or not 1 <= priority <= 5:
        raise ValueError('Notification configuration or message invalid')
    # RFC 2047 permits Unicode titles without invalid HTTP header bytes.
    from email.header import Header
    headers = {'Title': Header(title, 'utf-8').encode(), 'Priority': str(priority)}
    if os.environ.get('NTFY_TOKEN'):
        headers['Authorization'] = 'Bearer ' + os.environ['NTFY_TOKEN']
    try:
        with urllib.request.urlopen(urllib.request.Request(url, message.encode(), headers), timeout=20):
            pass
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise RuntimeError('Notification delivery could not be confirmed') from None
    return {'accepted': True}

if __name__ == '__main__':
    mcp.run(transport='stdio')
