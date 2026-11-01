import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from mcp.types import CallToolResult, TextContent
import pytest
from settings import Configuration, Sensor, Sampling
from sensors import normalize, collect_series
import sensors

def reading(sensor_id='door',age=0,reading=None):
    return CallToolResult(content=[TextContent(type='text',text=json.dumps({
        'sensor_id':sensor_id,'observed_at':(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(),
        'reading':reading if reading is not None else {'state':'closed'}}))])

@pytest.mark.parametrize('sensor_id,age',[('wrong',0),('door',-90)])
def test_invalid_observation(sensor_id,age):
    with pytest.raises(ValueError):normalize(reading(sensor_id,age),'door',Sensor(name='Door'))

def test_text_observation(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    metadata,content=normalize(reading(),'door',Sensor(name='Door',tool='read_door'))
    assert metadata['reading']=={'state':'closed'}
    assert metadata['images']==[]
    assert all(c['type']=='input_text' for c in content)


def test_future_timestamp_rejected():
    with pytest.raises(ValueError, match='future'):
        normalize(reading(age=-90), 'door', Sensor(name='Door'))


def test_old_observation_is_accepted_without_retimestamping(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    result = reading(age=86400 * 5)
    stamp = json.loads(result.content[0].text)['observed_at']
    metadata, _ = normalize(result, 'door', Sensor(name='Door'))
    assert metadata['observed_at'] == stamp
    assert metadata['reading']['state'] == 'closed'


@pytest.fixture
def door_config(tmp_path):
    server = tmp_path / 'sensor_server.py'
    server.write_text("""import json
from datetime import datetime, timezone
from mcp.server.fastmcp import FastMCP
mcp = FastMCP('Test sensor')
@mcp.tool()
def read_door(sensor_id: str) -> str:
    return json.dumps({'sensor_id': sensor_id,
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'reading': {'state': 'closed'}})
if __name__ == '__main__':
    mcp.run(transport='stdio')
""")
    return Configuration.model_validate({
        'servers': {'test': {'command': 'python', 'args': [str(server)]}},
        'sensors': {'coop_door': {'name': 'Door', 'server': 'test', 'tool': 'read_door'}},
        'scenes': {'door': {'name': 'Door', 'sensors': {'coop_door': {'evaluations': 1}},
                            'conditions': ['The door is closed.']}},
    })

def test_generic_door_end_to_end(tmp_path,monkeypatch,door_config):
    from agent import evaluate
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    config=door_config
    async def api(payload):
        text=payload['input'][0]['content'][-1]['text']
        evidence=json.loads(text)
        assert evidence['reading']['state']=='closed'
        response={'summary':'Closed','conditions':[{'condition':'The door is closed.',
            'status':'met','explanation':'Contact reading is closed.','evidence_ids':[evidence['evidence_id']]}]}
        return {'output':[{'content':[{'type':'output_text','text':json.dumps(response)}]}]}
    result=asyncio.run(evaluate(config.scenes['door'],config,lambda *a:None,api=api))
    assert result['status']=='normal'
    assert result['trace'][0]['tool']=='read_door'
    assert result['evidence'][0]['images']==[]

def test_missing_tool_is_unknown(tmp_path,monkeypatch,door_config):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    config=door_config
    config.sensors['coop_door'].tool='nonexistent'
    result=asyncio.run(collect_series('coop_door',Sampling(evaluations=2),config,lambda *a:None,[],[]))
    assert result==[None,None]


@pytest.fixture
def fake_mcp(monkeypatch):
    state = SimpleNamespace(result=reading('coop_door'), tools=['read_door'],
                            startup_error=None, call_error=None)
    @asynccontextmanager
    async def stdio(_):
        if state.startup_error:
            raise state.startup_error
        yield None, None
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def initialize(self): pass
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in state.tools])
        async def call_tool(self, *_):
            if state.call_error:
                raise state.call_error
            return state.result
    monkeypatch.setattr(sensors, 'stdio_client', stdio)
    monkeypatch.setattr(sensors, 'ClientSession', lambda *_: Session())
    return state


@pytest.mark.parametrize('failure,stage,diagnostic', [
    ('future', 'observation validation and storage', 'in the future'),
    ('invalid_timestamp', 'observation validation and storage', 'timestamp is missing or invalid'),
    ('tool_error', 'MCP tool invocation', 'MCP tool reported an error'),
    ('timeout', 'MCP tool invocation', 'Timed out waiting'),
    ('startup', 'MCP server startup', 'Executable or required file not found'),
    ('missing_tool', 'MCP tool discovery', 'Configured MCP tool was not found'),
    ('exception_group', 'MCP server startup', 'RuntimeError'),
])
def test_collection_failures_are_logged_and_saved_without_secrets(
        monkeypatch, tmp_path, door_config, fake_mcp, caplog, failure, stage, diagnostic):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    if failure == 'future':
        fake_mcp.result = reading('coop_door', age=-90)
    elif failure == 'invalid_timestamp':
        envelope = json.loads(fake_mcp.result.content[0].text)
        envelope['observed_at'] = 'SECRET-bad-timestamp'
        fake_mcp.result.content[0].text = json.dumps(envelope)
    elif failure == 'tool_error':
        fake_mcp.result = CallToolResult(isError=True, content=[
            TextContent(type='text', text='SECRET-vendor-response')])
    elif failure == 'timeout':
        fake_mcp.call_error = TimeoutError('SECRET-camera-url')
    elif failure == 'startup':
        fake_mcp.startup_error = FileNotFoundError('SECRET-executable-path')
    elif failure == 'missing_tool':
        fake_mcp.tools = []
    elif failure == 'exception_group':
        fake_mcp.startup_error = ExceptionGroup('SECRET', [RuntimeError('SECRET-vendor-data')])
    trace = []
    result = asyncio.run(collect_series('coop_door', Sampling(evaluations=1), door_config,
                                       lambda *a: None, trace, [], scene_name='Girls'))
    assert result == [None]
    assert 'scene=Girls sensor=coop_door server=test tool=read_door' in caplog.text
    assert f'stage={stage}' in caplog.text
    assert diagnostic in caplog.text
    assert trace[0]['status'] == 'failed'
    assert trace[0]['stage'] == stage
    assert diagnostic in trace[0]['error']
    assert 'SECRET' not in caplog.text + json.dumps(trace)


def test_invalid_timestamp_reason_survives_evaluation(monkeypatch, tmp_path, door_config, fake_mcp, caplog):
    from agent import evaluate
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    fake_mcp.result = reading('coop_door', age=-90)
    async def api(_):
        pytest.fail('Failed observations must not reach the model')
    result = asyncio.run(evaluate(door_config.scenes['door'], door_config, lambda *a: None, api=api))
    assert result['status'] == 'unknown'
    assert 'in the future' in result['conditions'][0]['explanation']
    assert result['trace'][0]['stage'] == 'observation validation and storage'
    assert 'Traceback' not in caplog.text
