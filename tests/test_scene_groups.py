import asyncio
import json

import pytest

from agent import evaluate, check_timeout
from settings import Configuration


def configuration():
    return Configuration.model_validate({
        'sensors': {'camera': {'name': 'Camera'}},
        'scenes': {
            'girls': {'name': 'Girls', 'sensors': {'camera': {'evaluations': 1}},
                      'conditions': ['Chickens visible.']},
            'chicky': {'name': 'Chicky', 'sensors': {'camera': {'evaluations': 1}},
                       'conditions': ['Chicken visible.']},
            'all': {'name': 'All', 'scenes': ['girls', 'chicky'],
                    'conditions': ['Scenes girls and chicky pass.']},
        },
    })


@pytest.mark.parametrize('children', [['missing'], ['all'], ['girls', 'girls']])
def test_invalid_children(children):
    raw = configuration().model_dump()
    raw['scenes']['all']['scenes'] = children
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)


def test_indirect_cycle():
    raw = configuration().model_dump()
    raw['scenes']['girls'] = {'name': 'Girls', 'scenes': ['all'], 'conditions': ['All pass.']}
    with pytest.raises(ValueError, match='Circular'):
        Configuration.model_validate(raw)


@pytest.mark.parametrize('sources', [{}, {'sensors': {'camera': {}}, 'scenes': ['girls']}])
def test_requires_exactly_one_source(sources):
    raw = configuration().model_dump()
    raw['scenes']['all'] = {'name': 'All', 'conditions': ['Pass.'], **sources}
    with pytest.raises(ValueError):
        Configuration.model_validate(raw)


@pytest.mark.parametrize('statuses', [('met', 'met'), ('met', 'unknown'), ('not_met', 'unknown')])
def test_parent_receives_fresh_child_results(statuses):
    config = configuration()
    calls = []
    async def sensor(scene, config, progress, api):
        calls.append(scene.name)
        status = statuses[0 if scene.name == 'Girls' else 1]
        return {'summary': scene.name + ' result', 'trace': [], 'evidence': [],
                'conditions': [{'condition': scene.conditions[0], 'status': status,
                                'explanation': 'Observed', 'evidence_ids': []}]}
    async def model(payload):
        data = json.loads(payload['input'])
        assert [c['scene_id'] for c in data['children']] == ['girls', 'chicky']
        expected = [{'met': 'normal', 'not_met': 'needs_attention', 'unknown': 'unknown'}[s] for s in statuses]
        assert [c['status'] for c in data['children']] == expected
        assert 'different child' in payload['instructions']
        status = 'not_met' if 'not_met' in statuses else 'unknown' if 'unknown' in statuses else 'met'
        return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
            'summary': 'Group result', 'conditions': [{'condition': data['conditions'][0],
                'status': status, 'explanation': 'Child results',
                'evidence_ids': ['scene:girls', 'scene:chicky']}]})}]}]}
    actual = asyncio.run(evaluate(config.scenes['all'], config, lambda *a: None, api=model, evaluator=sensor))
    assert calls == ['Girls', 'Chicky']
    assert len(actual['children']) == 2
    assert actual['summary'] == 'Group result'
    assert actual['status'] == ('needs_attention' if 'not_met' in statuses else 'unknown' if 'unknown' in statuses else 'normal')


def test_nested_groups_cache_shared_children_and_timeout():
    raw = configuration().model_dump()
    raw['scenes']['root'] = {'name': 'Root', 'scenes': ['girls', 'all'], 'conditions': ['All pass.']}
    config = Configuration.model_validate(raw)
    calls = []
    async def sensor(scene, config, progress, api):
        calls.append(scene.name)
        return {'summary': 'Pass', 'trace': [], 'evidence': [], 'conditions': [
            {'condition': scene.conditions[0], 'status': 'met', 'explanation': 'Observed', 'evidence_ids': []}]}
    async def model(payload):
        data = json.loads(payload['input'])
        return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
            'summary': 'Pass', 'conditions': [{'condition': data['conditions'][0], 'status': 'met',
                'explanation': 'Children passed', 'evidence_ids': [c['evidence_id'] for c in data['children']]}]})}]}]}
    result = asyncio.run(evaluate(config.scenes['root'], config, lambda *a: None, api=model, evaluator=sensor))
    assert calls == ['Girls', 'Chicky']
    assert result['status'] == 'normal'
    assert result['children'][1]['children'][0]['scene_id'] == 'girls'
    assert check_timeout(config.scenes['root'], config) == 720


def test_group_failure_preserves_children():
    config = configuration()
    async def sensor(*a, **kw):
        raise RuntimeError('Unavailable')
    async def model(payload):
        data = json.loads(payload['input'])
        assert all(c['status'] == 'unknown' for c in data['children'])
        raise RuntimeError('Model unavailable')
    result = asyncio.run(evaluate(config.scenes['all'], config, lambda *a: None, api=model, evaluator=sensor))
    assert result['status'] == 'unknown'
    assert len(result['children']) == 2
    assert all(c['status'] == 'unknown' for c in result['children'])


def test_parent_rest_and_persistence(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from web import app
    config = configuration()
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr('web.load_config', lambda: config)
    async def no_planning(config):
        return config
    monkeypatch.setattr('web.resolve_plans', no_planning)
    async def sensor(scene, config, progress, api):
        return {'summary': 'Pass', 'trace': [], 'evidence': [], 'conditions': [
            {'condition': scene.conditions[0], 'status': 'met', 'explanation': 'Observed', 'evidence_ids': []}]}
    async def model(payload):
        data = json.loads(payload['input'])
        return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
            'summary': 'Both passed', 'conditions': [{'condition': data['conditions'][0],
                'status': 'met', 'explanation': 'Both children passed',
                'evidence_ids': ['scene:girls', 'scene:chicky']}]})}]}]}
    async def run(scene, config, progress):
        return await evaluate(scene, config, progress, api=model, evaluator=sensor)
    monkeypatch.setattr('web.evaluate', run)
    import time
    with TestClient(app) as client:
        assert client.get('/api/scenes').json()['scenes']['all']['scenes'] == ['girls', 'chicky']
        response = client.post('/api/scenes/all/checks')
        assert response.status_code == 202
        check_id = response.json()['id']
        for _ in range(100):
            record = client.get('/api/checks/' + check_id).json()
            if record['state'] == 'finished':
                break
            time.sleep(.01)
        assert record['status'] == 'normal'
        assert len(record['children']) == 2
    with TestClient(app) as client:
        record = client.get('/api/checks/' + check_id).json()
        assert record['children'][0]['scene_id'] == 'girls'
        assert record['summary'] == 'Both passed'
