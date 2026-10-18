import asyncio
import json
from datetime import datetime, timezone, timedelta
from mcp.types import CallToolResult, TextContent
import pytest
from settings import Configuration, Sensor, Sampling
from sensors import normalize, collect_series

def reading(sensor_id='door',age=0,reading=None):
    return CallToolResult(content=[TextContent(type='text',text=json.dumps({
        'sensor_id':sensor_id,'observed_at':(datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(),
        'reading':reading if reading is not None else {'state':'closed'}}))])

@pytest.mark.parametrize('sensor_id,age',[('wrong',0),('door',500),('door',-90)])
def test_invalid_observation(sensor_id,age):
    with pytest.raises(ValueError):normalize(reading(sensor_id,age),'door',Sensor(name='Door'))

def test_text_observation(tmp_path,monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    metadata,content=normalize(reading(),'door',Sensor(name='Door',tool='read_door'))
    assert metadata['reading']=={'state':'closed'}
    assert metadata['images']==[]
    assert all(c['type']=='input_text' for c in content)


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
