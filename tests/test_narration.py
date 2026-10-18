import asyncio
import json

import pytest

from narration import write_report, fallback_report
from actions import run_actions, render
from action_planner import resolve_action_plans
from test_action_planner import config, catalog, output


def record():
    return {'id': 'check', 'scene_id': 'coop', 'scene_name': 'All coops', 'created_at': 'now',
            'status': 'needs_attention', 'summary': 'Some conditions failed', 'conditions': [],
            'trace': ['SECRET'], 'evidence': [{'image': 'SECRET'}],
            'children': [{'name': 'Chicky', 'status': 'needs_attention',
                          'conditions': [
                              {'condition': 'Door closed', 'status': 'not_met',
                               'explanation': 'The door sensor reports open.'},
                              {'condition': 'One chicken visible', 'status': 'unknown',
                               'explanation': 'The image is too dark to count chickens.'}],
                          'sensors': [{'name': 'Camera', 'status': 'unknown', 'conditions': [],
                                       'evaluations': [{'status': 'unknown', 'conditions': [],
                                                        'evidence': ['SECRET']}]}],
                          'trace': ['SECRET'], 'evidence': ['SECRET']}]}


def response(message):
    return {'output': [{'content': [{'type': 'output_text', 'text': json.dumps({'message': message})}]}]}


def test_writer_uses_actual_child_findings_and_preserves_uncertainty_in_prompt():
    payloads = []
    message = "Chicky's door is open. It was too dark to confirm how many chickens were visible."
    async def api(payload):
        payloads.append(payload)
        return response(message)
    assert asyncio.run(write_report(record(), api)) == message
    payload = payloads[0]
    assert payload['store'] is False and 'tools' not in payload
    assert 'SECRET' not in payload['input']
    assert 'image is too dark' in payload['input']
    assert 'does NOT mean zero' in payload['instructions']
    assert 'couldn\'t confirm' in payload['instructions']


@pytest.mark.parametrize('message', ['', '  ', 'x' * 1501])
def test_invalid_report_fails(message):
    async def api(payload): return response(message)
    with pytest.raises(ValueError):
        asyncio.run(write_report(record(), api))


def test_startup_compiles_plain_english_template_without_writing_live_results():
    c = config('Turn Hue red, then send me a phone notification with the scene result in plain english.')
    async def api(payload):
        assert 'result.plain_english' in payload['instructions']
        return output([{'action_id': 'light', 'arguments_json': '{"hue":0}'},
                       {'action_id': 'phone', 'arguments_json': '{"message":"{{ result.plain_english }}"}'}])
    asyncio.run(resolve_action_plans(c, api, catalog))
    assert c.scenes['coop'].on.not_normal._steps[1].arguments == {'message': '{{ result.plain_english }}'}


@pytest.mark.parametrize('writer_fails', [False, True])
def test_runtime_generates_once_after_light_then_delivers_even_when_writing_fails(writer_fails):
    c = config()
    c.scenes['coop'].on.not_normal = ['light', 'phone', 'phone2']
    c.actions['phone'].arguments['message'] = '{{ result.plain_english }}'
    c.actions['phone2'] = c.actions['phone'].model_copy()
    rec, events, snapshots = record(), [], []
    message = "Chicky's door is open; the chicken count couldn't be confirmed."
    async def writer(current):
        events.append('write')
        assert current['children'][0]['conditions'][1]['status'] == 'unknown'
        if writer_fails:
            raise RuntimeError('SECRET API response')
        return message
    async def executor(action_id, effective, context):
        events.append(action_id)
        if action_id.startswith('phone'):
            text = render(effective.actions[action_id].arguments, context)['message']
            assert text == (fallback_report(rec) if writer_fails else message)
        return {'status': 'failed' if action_id == 'light' else 'succeeded'}
    asyncio.run(run_actions(rec, c.scenes['coop'], c, lambda r: snapshots.append(json.loads(json.dumps(r))),
                           executor, writer))
    assert events == ['light', 'write', 'phone', 'phone2']
    assert rec['report_source'] == ('fallback' if writer_fails else 'model')
    assert rec['status'] == 'needs_attention'
    assert rec['actions'][-1]['status'] == 'succeeded'
    assert 'SECRET API response' not in json.dumps(snapshots)


def test_no_writer_for_literal_or_legacy_messages():
    c = config()
    c.scenes['coop'].on.not_normal = ['phone']
    async def writer(rec): pytest.fail('No report template requested')
    async def executor(*args): return {'status': 'succeeded'}
    asyncio.run(run_actions(record(), c.scenes['coop'], c, lambda r: None, executor, writer))


def test_cancel_writing_does_not_send_notification():
    c = config()
    c.scenes['coop'].on.not_normal = ['phone']
    c.actions['phone'].arguments['message'] = '{{ result.plain_english }}'
    async def writer(rec): raise asyncio.CancelledError()
    async def executor(*args): pytest.fail('Cancelled writing must not deliver')
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_actions(record(), c.scenes['coop'], c, lambda r: None, executor, writer))


def test_scene_style_reaches_writer_and_does_not_leak_to_abnormal_trigger():
    from settings import ActionInstructions
    from action_planner import describe_action_plans
    c = config()
    style = 'Keep it brief. Avoid listing each coop or repeating that nothing went wrong.'
    c.scenes['coop'].on.normal = ActionInstructions(instructions='Send a notification. ' + style, actions=['phone'])
    c.scenes['coop'].on.not_normal.actions = ['phone']
    async def planner_api(payload):
        data = json.loads(payload['input'])
        return output([{'action_id': 'phone', 'arguments_json': '{"message":"{{ result.plain_english }}"}',
                        'writing_instructions': style if data['status'] == 'normal' else ''}])
    asyncio.run(resolve_action_plans(c, planner_api, catalog))
    assert describe_action_plans(c.scenes['coop'])['normal']['steps'][0]['writing_instructions'] == style
    delivered, preferences = [], []
    async def writer_api(payload):
        data = json.loads(payload['input'])
        preferences.append(data['writing_instructions'])
        assert 'findings' in data and 'must not select actions' in payload['instructions']
        return response('All good. All chickens accounted for and doors closed.' if data['writing_instructions']
                        else "Chicky's door is open; the chicken count couldn't be confirmed.")
    async def writer(current): return await write_report(current, writer_api)
    async def executor(action_id, effective, context):
        delivered.append(render(effective.actions[action_id].arguments, context)['message'])
        return {'status': 'succeeded'}
    normal = record() | {'status': 'normal', 'summary': 'All confirmed', 'children': []}
    asyncio.run(run_actions(normal, c.scenes['coop'], c, lambda r: None, executor, writer))
    abnormal = record()
    asyncio.run(run_actions(abnormal, c.scenes['coop'], c, lambda r: None, executor, writer))
    assert preferences == [style, '']
    assert delivered[0].startswith('All good.')
    assert 'door is open' in delivered[1]
    assert normal['actions'][0]['report'] == delivered[0]


def test_distinct_writing_styles_have_distinct_cached_reports():
    from settings import ActionInstructions, ActionStep
    c = config()
    rule = ActionInstructions(instructions='Notify twice', actions=['phone'])
    rule._steps = [ActionStep(action_id='phone', arguments={'message': '{{ result.plain_english }}'},
                              writing_instructions=style) for style in ['Brief', 'Detailed', 'Brief']]
    c.scenes['coop'].on.not_normal = rule
    calls, messages = [], []
    async def writer(current):
        calls.append(current['writing_instructions'])
        return current['writing_instructions'] + ' message'
    async def executor(action_id, effective, context):
        messages.append(render(effective.actions[action_id].arguments, context)['message'])
        return {'status': 'succeeded'}
    asyncio.run(run_actions(record(), c.scenes['coop'], c, lambda r: None, executor, writer))
    assert calls == ['Brief', 'Detailed']
    assert messages == ['Brief message', 'Detailed message', 'Brief message']


def test_writing_preferences_require_report_template():
    c = config()
    async def api(payload):
        return output([{'action_id': 'light', 'arguments_json': '{}', 'writing_instructions': 'Keep it brief'}])
    with pytest.raises(ValueError):
        asyncio.run(resolve_action_plans(c, api, catalog))
