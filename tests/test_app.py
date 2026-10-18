import asyncio
import json
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from agent import validate_result
from settings import Scene, Configuration, ROOT
from web import app

SCENE = Scene(name='Test', sensors={'camera': {'evaluations':1}}, conditions=['Three chickens and no dog'])
EVIDENCE = [{'sensor_id': 'camera', 'evidence_id': 'a.jpg'}]

def result(status='met', ids=None):
    return {'summary': 'Observation', 'conditions': [{'condition': SCENE.conditions[0],
        'status': status, 'explanation': 'Observed', 'evidence_ids': ['a.jpg'] if ids is None else ids}]}

@pytest.mark.parametrize('status,expected', [('met','normal'),('not_met','needs_attention'),('unknown','unknown')])
def test_aggregate(status, expected):
    assert validate_result(result(status), SCENE, EVIDENCE)['status'] == expected

@pytest.mark.parametrize('raw,evidence', [(result(), []), (result(ids=['invented']), EVIDENCE), (result(ids=[]), EVIDENCE)])
def test_no_unsupported_success(raw,evidence):
    with pytest.raises(ValueError):
        validate_result(raw, SCENE, evidence)

def test_missing_condition():
    with pytest.raises(ValueError):
        validate_result({'summary':'fine','conditions':[]},SCENE,EVIDENCE)

def test_invalid_scene():
    with pytest.raises(ValueError):
        Configuration.model_validate({'sensors':{},'scenes':{'bad': SCENE.model_dump()}})

def test_rest_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    config_path = tmp_path/'scenes.yaml'
    config_path.write_text(json.dumps({
        'sensors': {'camera': {'name': 'Test camera'}},
        'scenes': {'coop': SCENE.model_dump()},
    }))
    monkeypatch.setenv('SCENES_FILE',str(config_path))
    async def fake(scene, config, progress):
        await asyncio.sleep(.02)
        return {'status':'normal','summary':'All conditions met','conditions':[], 'evidence':[], 'trace':[]}
    async def no_planning(config): return config
    monkeypatch.setattr('web.resolve_plans',no_planning)
    with patch('web.evaluate',fake), TestClient(app) as client:
        assert client.get('/').status_code==200
        assert client.get('/docs').status_code==200
        assert client.post('/api/scenes/missing/checks').status_code==404
        response=client.post('/api/scenes/coop/checks')
        assert response.status_code==202
        check_id=response.json()['id']
        assert client.post('/api/scenes/coop/checks').status_code==409
        import time
        for _ in range(100):
            record=client.get('/api/checks/'+check_id).json()
            if record['state']=='finished':break
            time.sleep(.01)
        assert record['status']=='normal'
        assert client.get('/frames/not-a-frame.jpg').status_code==404
    path=tmp_path/'checks'/('a'*32+'.json')
    path.write_text(json.dumps({'id':'a'*32,'state':'running','created_at':'2026'}))
    with TestClient(app) as client:
        assert client.get('/api/checks/'+'a'*32).json()['status']=='unknown'

@pytest.mark.parametrize('mode',['discovery','image'])
def test_real_mcp(tmp_path,mode):
    # Real stdio client/server, including SDK image conversion. A synthetic FFmpeg
    # executable avoids contacting a camera while exercising the capture boundary.
    fake=tmp_path/'ffmpeg'
    fake.write_text('#!/usr/bin/env python3\nimport sys,base64\nfrom pathlib import Path\nPath(sys.argv[-1]).write_bytes(base64.b64decode("/9j/2Q=="))\n')
    fake.chmod(0o755)
    async def exercise():
        env={**os.environ,'PATH':str(tmp_path)+os.pathsep+os.environ['PATH'],
             'DATA_DIR':str(tmp_path/'data'),'SCENES_FILE':str(ROOT/'scenes.yaml'),
             'SCENE_SENSOR_IDS':'["coop_camera"]','CAMERA_1_RTSP_URL':'rtsp://test'}
        import sys
        async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m', 'mcp_servers.camera_server'],env=env,cwd=str(ROOT))) as (r,w):
            async with ClientSession(r,w) as session:
                await session.initialize()
                tools=await session.list_tools()
                assert tools.tools[0].name=='capture_camera'
                result=await session.call_tool('capture_camera',{'sensor_id':'coop_camera' if mode=='image' else 'unassigned','rtsp_env':'CAMERA_1_RTSP_URL'})
                if mode=='image':
                    assert not result.isError
                    assert any(b.type=='image' for b in result.content)
                    metadata=json.loads(next(b.text for b in result.content if b.type=='text'))
                    assert metadata['sensor_id']=='coop_camera'
                else:
                    assert result.isError
    asyncio.run(exercise())

def test_per_sensor_sampling_and_independence():
    from agent import evaluate
    from settings import Sensor, Sampling
    scene=Scene(name='Mixed', sensors={'a':Sampling(evaluations=4,interval_seconds=10),
                                     'b':Sampling(evaluations=1)},conditions=['Birds present','Lights off'])
    config=Configuration(sensors={s:Sensor(name=s,rtsp_env=s) for s in scene.sensors},scenes={'mixed':scene})
    calls, waits = [], []
    async def fake(single,config,progress,api):
        sensor=next(iter(single.sensors))
        calls.append(sensor)
        statuses=['met','not_applicable'] if sensor=='a' else ['not_applicable','met']
        return {'status':'normal','trace':[],'evidence':[], 'conditions':[
            {'condition':c,'status':s,'explanation':'Checked','evidence_ids':[]}
            for c,s in zip(single.conditions,statuses)]}
    async def sleep(seconds):waits.append(seconds)
    result=asyncio.run(evaluate(scene,config,lambda *args:None,evaluator=fake,sleep=sleep))
    assert calls==['a','a','a','a','b']
    assert waits==[10,10,10]
    assert result['status']=='normal'
    assert [len(s['evaluations']) for s in result['sensors']]==[4,1]

@pytest.mark.parametrize('statuses,expected', [(['met','unknown'],'unknown'),
    (['met','not_met'],'not_met'),(['not_applicable','met'],'unknown'),
    (['not_applicable','not_applicable'],'not_applicable')])
def test_repeated_aggregation(statuses,expected):
    from agent import aggregate_conditions
    assessments=[result(status=s) for s in statuses]
    assert aggregate_conditions(SCENE.conditions,assessments)[0]['status']==expected

def test_failed_evaluation_is_not_dropped():
    from agent import evaluate
    from settings import Sensor, Sampling
    scene=Scene(name='Failure',sensors={'camera':Sampling(evaluations=2)},conditions=['Birds present'])
    config=Configuration(sensors={'camera':Sensor(name='Camera',rtsp_env='URL')},scenes={'s':scene})
    async def fail(*args,**kwargs):raise RuntimeError('Camera failure')
    actual=asyncio.run(evaluate(scene,config,lambda *a:None,evaluator=fail))
    assert actual['status']=='unknown'
    assert len(actual['sensors'][0]['evaluations'])==2

@pytest.mark.parametrize('statuses,expected', [
    (['unknown', 'met'], 'met'),
    (['met', 'unknown'], 'met'),
    (['met', 'met'], 'met'),
    (['not_met', 'met'], 'not_met'),
    (['met', 'not_met'], 'not_met'),
    (['not_met', 'unknown'], 'not_met'),
    (['unknown', 'unknown'], 'unknown'),
    (['not_applicable', 'met'], 'met'),
    (['not_applicable', 'unknown'], 'unknown'),
    (['not_applicable', 'not_applicable'], 'unknown'),
])
def test_cross_sensor_aggregation(statuses, expected):
    from agent import evaluate
    from settings import Sensor, Sampling
    scene = Scene(name='Door', sensors={'camera': Sampling(), 'door': Sampling()},
                  conditions=['The door is closed.'])
    config = Configuration(sensors={s: Sensor(name=s) for s in scene.sensors}, scenes={'door': scene})
    findings = dict(zip(scene.sensors, statuses))
    async def fake(single, config, progress, api):
        sensor = next(iter(single.sensors))
        return {'trace': [], 'evidence': [], 'conditions': [{
            'condition': scene.conditions[0], 'status': findings[sensor],
            'explanation': sensor + ' observation', 'evidence_ids': [sensor],
        }]}
    actual = asyncio.run(evaluate(scene, config, lambda *a: None, evaluator=fake))
    assert actual['conditions'][0]['status'] == expected
    assert actual['status'] == {'met': 'normal', 'not_met': 'needs_attention', 'unknown': 'unknown'}[expected]
    assert [s['conditions'][0]['status'] for s in actual['sensors']] == statuses
    assert len(actual['sensors']) == 2

@pytest.mark.parametrize('recover', [True, False])
def test_invalid_assessment_retries_same_observation(recover, monkeypatch):
    from agent import evaluate_once
    from settings import Sensor
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    config = Configuration(sensors={'camera': Sensor(name='Camera')}, scenes={'test': SCENE})
    calls = []
    async def api(payload):
        calls.append(json.loads(json.dumps(payload)))
        raw = result()
        if len(calls) == 1 or not recover:
            raw['conditions'][0]['condition'] += '.'
        return {'model': 'test-model', 'output': [{'content': [
            {'type': 'output_text', 'text': json.dumps(raw)}]}]}
    observation = (EVIDENCE[0], [{'type': 'input_text', 'text': 'original observation'}])
    async def run():
        return await evaluate_once(SCENE, config, lambda *a: None, api=api, observation=observation)
    if recover:
        assert asyncio.run(run())['status'] == 'normal'
    else:
        with pytest.raises(ValueError, match='every configured condition'):
            asyncio.run(run())
    assert len(calls) == 2
    assert calls[0]['input'][0] == calls[1]['input'][0]
    assert calls[1]['input'][-1]['role'] == 'user'


def test_api_failure_is_not_retried_as_invalid_assessment(monkeypatch):
    from agent import evaluate_once
    from settings import Sensor
    monkeypatch.setenv('OPENAI_MODEL', 'test-model')
    config = Configuration(sensors={'camera': Sensor(name='Camera')}, scenes={'test': SCENE})
    calls = []
    async def api(payload):
        calls.append(payload)
        raise RuntimeError('OpenAI request failed (HTTP 401)')
    with pytest.raises(RuntimeError, match='HTTP 401'):
        asyncio.run(evaluate_once(SCENE, config, lambda *a: None, api=api,
                                 observation=(EVIDENCE[0], [])))
    assert len(calls) == 1
