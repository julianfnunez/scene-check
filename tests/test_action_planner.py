import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from action_planner import discover, resolve_action_plans, describe_action_plans
from actions import run_actions, selected_steps
from settings import Configuration


def config(instructions='Turn Hue dark green then send me a phone notification.'):
    return Configuration.model_validate({
        'servers': {'alerts': {'args': ['-m', 'mcp_servers.alert_server']}},
        'sensors': {'cam': {'name': 'Cam', 'server': 'alerts'}},
        'actions': {
            'light': {'name': 'Hue light', 'server': 'alerts', 'tool': 'set_light',
                      'arguments': {'on': True}, 'editable_arguments': ['hue', 'brightness']},
            'phone': {'name': 'Phone notification', 'server': 'alerts', 'tool': 'publish',
                      'arguments': {'message': '{{ result.summary }}', 'topic_env': 'PRIVATE_TOPIC'},
                      'editable_arguments': ['message']},
            'off': {'name': 'Off', 'server': 'alerts', 'tool': 'set_light', 'arguments': {'on': False}},
        },
        'scenes': {'coop': {'name': 'Coop', 'sensors': {'cam': {}}, 'conditions': ['Birds visible'],
                           'on': {'not_normal': {'instructions': instructions, 'actions': ['light', 'phone']}}}},
    })


CATALOG = {
    'set_light': {'description': 'Hue green is 25500; dark brightness is 32.',
                  'input_schema': {'type': 'object', 'required': ['on'], 'additionalProperties': False,
                                   'properties': {'on': {'type': 'boolean'},
                                                  'hue': {'type': 'integer', 'minimum': 0, 'maximum': 65535},
                                                  'brightness': {'type': 'integer', 'minimum': 1, 'maximum': 254}}}},
    'publish': {'description': 'Send phone notification.',
                'input_schema': {'type': 'object', 'required': ['message'], 'additionalProperties': False,
                                 'properties': {'message': {'type': 'string'}, 'topic_env': {'type': 'string'}}}},
}


def output(steps=None, supported=True):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({
        'supported': supported, 'reason': '', 'steps': [dict(s, writing_instructions=s.get('writing_instructions', '')) for s in (steps if steps is not None else [
            {'action_id': 'light', 'arguments_json': '{"hue":25500,"brightness":32}'},
            {'action_id': 'phone', 'arguments_json': '{}'},
        ])]})}]}]}


async def catalog(server): return CATALOG


def test_compile_order_execute_and_no_planning_side_effects():
    c, requests, discoveries = config(), [], []
    async def api(payload):
        requests.append(payload)
        return output()
    async def discovery(server):
        discoveries.append(server)
        return CATALOG
    asyncio.run(resolve_action_plans(c, api, discovery))
    assert len(discoveries) == 1
    assert requests[0]['store'] is False
    assert requests[0]['text']['format']['strict'] is True
    assert 'PRIVATE_TOPIC' not in requests[0]['input']
    steps = selected_steps(c.scenes['coop'], 'unknown')
    assert [s.action_id for s in steps] == ['light', 'phone']
    assert steps[0].arguments == {'hue': 25500, 'brightness': 32}
    calls = []
    async def executor(action_id, effective, context):
        calls.append((action_id, effective.actions[action_id].arguments))
        return {'status': 'failed' if action_id == 'light' else 'succeeded'}
    rec = {'id': 'test', 'scene_id': 'coop', 'scene_name': 'Coop', 'created_at': 'now',
           'status': 'unknown', 'summary': 'Dark', 'conditions': []}
    asyncio.run(run_actions(rec, c.scenes['coop'], c, lambda r: None, executor))
    assert calls == [('light', {'on': True, 'hue': 25500, 'brightness': 32}),
                     ('phone', {'message': '{{ result.summary }}', 'topic_env': 'PRIVATE_TOPIC'})]
    assert c.actions['light'].arguments == {'on': True}
    assert describe_action_plans(c.scenes['coop'])['not_normal']['steps'][0]['arguments']['brightness'] == 32


@pytest.mark.parametrize('steps', [
    [],
    [{'action_id': 'off', 'arguments_json': '{}'}],
    [{'action_id': 'light', 'arguments_json': '{"on":false}'}],
    [{'action_id': 'light', 'arguments_json': '{"hue":999999}'}],
    [{'action_id': 'light', 'arguments_json': '{"brightness":"dark"}'}],
    [{'action_id': 'phone', 'arguments_json': '{"topic_env":"OTHER_TOPIC"}'}],
    [{'action_id': 'phone', 'arguments_json': '{"message":"{{ result.secret }}"}'}],
    [{'action_id': 'light', 'arguments_json': '[]'}],
    [{'action_id': 'light', 'arguments_json': 'broken'}],
    [{'action_id': 'phone', 'arguments_json': '{}'}] * 13,
])
def test_invalid_plan_blocks_without_publishing(steps):
    c = config()
    async def api(payload): return output(steps)
    with pytest.raises(ValueError, match='coop/on/not_normal'):
        asyncio.run(resolve_action_plans(c, api, catalog))
    assert describe_action_plans(c.scenes['coop'])['not_normal']['steps'] is None
    with pytest.raises(ValueError, match='not been resolved'):
        selected_steps(c.scenes['coop'], 'unknown')


def test_unsupported_and_missing_tool():
    async def api(payload): return output(supported=False)
    with pytest.raises(ValueError):
        asyncio.run(resolve_action_plans(config('Wait five minutes then notify'), api, catalog))
    async def missing(server): return {}
    with pytest.raises(ValueError):
        asyncio.run(resolve_action_plans(config(), api, missing))


def test_repeated_steps_preserved_and_all_plans_publish_atomically():
    c = config()
    async def api(payload):
        return output([{'action_id': 'light', 'arguments_json': '{"hue":25500}'},
                       {'action_id': 'light', 'arguments_json': '{"hue":0}'}])
    asyncio.run(resolve_action_plans(c, api, catalog))
    assert len(selected_steps(c.scenes['coop'], 'unknown')) == 2
    from settings import ActionInstructions
    c.scenes['coop'].on.normal = ActionInstructions(instructions='Notify', actions=['phone'])
    responses = iter([output([{'action_id': 'phone', 'arguments_json': '{}'}]), output(supported=False)])
    async def fail_later(payload): return next(responses)
    with pytest.raises(ValueError):
        asyncio.run(resolve_action_plans(c, fail_later, catalog))
    assert c.scenes['coop'].on.normal._steps is None
    assert c.scenes['coop'].on.not_normal._steps is None


def test_explicit_only_does_not_discover_or_call_model():
    c = config()
    c.scenes['coop'].on.not_normal = ['light', 'phone']
    async def forbidden(*args): pytest.fail('No planning calls needed')
    asyncio.run(resolve_action_plans(c, forbidden, forbidden))


@pytest.mark.parametrize('explicit_override', [False, True])
def test_english_defaults_compile_for_each_status_and_scene_override(explicit_override):
    c = config('Send phone notification' + (' at priority 5.' if explicit_override else '.'))
    from settings import ActionInstructions
    c.scenes['coop'].on.not_normal.actions = ['phone']
    c.scenes['coop'].on.normal = ActionInstructions(instructions='Send phone notification'
        + (' at priority 5.' if explicit_override else '.'), actions=['phone'])
    c.actions['phone'].arguments = {'topic_env': 'PRIVATE_TOPIC'}
    c.actions['phone'].default_instructions = 'Write in plain English. Use priority 3 for normal and 4 for abnormal.'
    c.actions['phone'].editable_arguments = ['message', 'priority']
    calls = []
    async def discovery(server):
        import copy
        result = copy.deepcopy(CATALOG)
        result['publish']['input_schema']['properties']['priority'] = {'type': 'integer', 'minimum': 1, 'maximum': 5}
        return result
    async def api(payload):
        data = json.loads(payload['input'])
        calls.append(data['status'])
        assert data['actions']['phone']['default_instructions'].startswith('Write in plain English')
        assert 'Explicit scene instructions take precedence' in payload['instructions']
        priority = 5 if 'priority 5' in data['instructions'] else (3 if data['status'] == 'normal' else 4)
        return output([{'action_id': 'phone', 'arguments_json': json.dumps({
            'message': '{{ result.plain_english }}', 'priority': priority})}])
    asyncio.run(resolve_action_plans(c, api, discovery))
    assert calls == ['normal', 'needs_attention', 'unknown']
    for status in calls:
        step = selected_steps(c.scenes['coop'], status)[0]
        assert step.arguments['priority'] == (5 if explicit_override else (3 if status == 'normal' else 4))
    plans = describe_action_plans(c.scenes['coop'])
    assert set(plans['not_normal']['steps_by_status']) == {'needs_attention', 'unknown'}
    assert c.actions['phone'].arguments == {'topic_env': 'PRIVATE_TOPIC'}


def test_action_defaults_also_compile_explicit_lists():
    c = config()
    c.scenes['coop'].on.not_normal = ['phone']
    c.actions['phone'].default_instructions = 'Write in plain English.'
    calls = []
    async def api(payload):
        calls.append(json.loads(payload['input'])['status'])
        return output([{'action_id': 'phone', 'arguments_json': '{"message":"{{ result.plain_english }}"}'}])
    asyncio.run(resolve_action_plans(c, api, catalog))
    assert calls == ['needs_attention', 'unknown']
    assert selected_steps(c.scenes['coop'], 'unknown')[0].arguments['message'] == '{{ result.plain_english }}'


def test_invalid_default_argument_blocks_all_status_plans():
    c = config()
    c.actions['phone'].default_instructions = 'Write in plain English.'
    calls = []
    async def api(payload):
        calls.append(1)
        return output([{'action_id': 'phone', 'arguments_json': '{}'}] if len(calls) == 1 else
                      [{'action_id': 'phone', 'arguments_json': '{"topic_env":"UNAUTHORIZED"}'}])
    with pytest.raises(ValueError):
        asyncio.run(resolve_action_plans(c, api, catalog))
    rule = c.scenes['coop'].on.not_normal
    assert rule._steps is None
    assert rule._steps_by_status == {}


@pytest.mark.parametrize('defaults,manual', [(' ', False), ('Use priority 3', True)])
def test_invalid_default_configuration(defaults, manual):
    from settings import Action
    with pytest.raises(ValueError):
        Action(name='Phone', server='alerts', tool='publish', default_instructions=defaults, manual=manual)


def test_explicit_lists_keep_order_and_deduplicate_across_triggers():
    c = config()
    c.scenes['coop'].on.unknown = ['phone']
    c.scenes['coop'].on.not_normal = ['phone', 'light']
    c.actions['phone'].default_instructions = 'Write in plain English'
    async def api(payload):
        data = json.loads(payload['input'])
        return output([{'action_id': aid, 'arguments_json': '{}'} for aid in data['actions']])
    asyncio.run(resolve_action_plans(c, api, catalog))
    assert [s.action_id for s in selected_steps(c.scenes['coop'], 'unknown')] == ['phone', 'light']


def test_real_tool_discovery_is_read_only():
    c = config()
    tools = asyncio.run(discover(c.servers['alerts']))
    schema = tools['set_light']['input_schema']
    assert schema['properties']['brightness']['maximum'] == 254
    assert 'dark' in tools['set_light']['description'].lower()


@pytest.mark.parametrize('invalid', [True, False])
def test_startup_plan_and_error_visible(tmp_path, monkeypatch, invalid):
    import web
    c = config()
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(web, 'load_config', lambda: c)
    async def observations(c): return c
    async def api(payload): return output(supported=not invalid)
    async def actions(c): return await resolve_action_plans(c, api, catalog)
    monkeypatch.setattr(web, 'resolve_plans', observations)
    monkeypatch.setattr(web, 'resolve_action_plans', actions)
    with TestClient(web.app) as client:
        data = client.get('/api/scenes').json()
        plans = data['scenes']['coop']['action_plans']
        if invalid:
            assert data['config_error']
            assert plans['not_normal']['steps'] is None
            assert client.post('/api/scenes/coop/checks', headers={'X-Check-Token': data['token']}).status_code == 422
        else:
            assert data['config_error'] is None
            assert plans['not_normal']['steps'][0]['action_id'] == 'light'
