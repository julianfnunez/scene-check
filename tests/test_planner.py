import asyncio
import json
import pytest
from settings import Configuration, Sampling
from planner import resolve_plans

def config():
    return Configuration.model_validate({'sensors':{'cam':{'name':'Camera','rtsp_env':'URL'}},
        'scenes':{'test':{'name':'Test','sensors':{'cam':{'instructions':'4 captures 10 seconds apart'}},'conditions':['Lights off']}}})

def response(plan):
    return {'output':[{'content':[{'type':'output_text','text':json.dumps(plan)}]}]}

def test_translation():
    calls=[]
    async def api(payload):
        calls.append(payload)
        return response(dict(supported=True,reason='',evaluations=4,interval_seconds=10))
    c=asyncio.run(resolve_plans(config(),api))
    plan=c.scenes['test'].sensors['cam']
    assert (plan.evaluations,plan.interval_seconds)==(4,10)
    assert plan.instructions=='4 captures 10 seconds apart'
    assert len(calls)==1

@pytest.mark.parametrize('plan',[
    dict(supported=False,reason='Ambiguous timing',evaluations=1,interval_seconds=0),
    dict(supported=True,reason='',evaluations=99,interval_seconds=10)])
def test_rejects_unsupported(plan):
    async def api(payload):return response(plan)
    with pytest.raises(ValueError):asyncio.run(resolve_plans(config(),api))

def test_no_mixed_settings():
    with pytest.raises(ValueError):Sampling(instructions='four captures',evaluations=4)

def test_supplied_capture_is_independent(monkeypatch):
    from agent import evaluate_once
    c=config()
    seen=[]
    async def api(payload):
        seen.append(payload)
        return response({'summary':'Off','conditions':[{'condition':'Lights off','status':'met','explanation':'Observed','evidence_ids':['a.jpg']}]})
    metadata={'sensor_id':'cam','evidence_id':'a.jpg'}
    observation=(metadata,[{'type':'input_text','text':json.dumps(metadata)}, {'type':'input_image','image_url':'data:image/jpeg;base64,AA==','detail':'high'}])
    for _ in range(2):
        result=asyncio.run(evaluate_once(c.scenes['test'],c,lambda *a:None,api=api,observation=observation))
        assert result['status']=='normal'
    assert all(len(p['input'])==1 and 'tools' not in p for p in seen)

def test_capture_series_mcp(tmp_path,monkeypatch):
    import os
    from datetime import datetime
    from sensors import collect_series
    from settings import load_config
    from settings import ROOT
    executable=tmp_path/'ffmpeg'
    executable.write_text('#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nPath(sys.argv[-1]).write_bytes(b"jpeg")\n')
    executable.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+os.pathsep+os.environ['PATH'])
    monkeypatch.setenv('DATA_DIR',str(tmp_path/'data'))
    monkeypatch.setenv('SCENES_FILE',str(ROOT/'scenes.yaml'))
    monkeypatch.setenv('CAMERA_1_RTSP_URL','rtsp://test')
    observations=asyncio.run(collect_series('coop_camera',Sampling(evaluations=4,interval_seconds=.1),load_config(),lambda *a:None,[],[]))
    assert len(observations)==4
    assert all(o is not None for o in observations)
    assert len({o[0]['evidence_id'] for o in observations})==4
    times=[datetime.fromisoformat(o[0]['captured_at']) for o in observations]
    assert (times[-1]-times[0]).total_seconds()>=.25
