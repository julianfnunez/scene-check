"""Generic, single-attempt MCP actions. Tool output never becomes instructions."""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from jsonschema import validate
from settings import ROOT, ActionInstructions, ActionStep
from narration import write_report, fallback_report

TEMPLATE = re.compile(r'{{\s*([a-zA-Z_][\w.]*)\s*}}')
logger = logging.getLogger(__name__)


def render(value, context):
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str):
        return value
    def lookup(match):
        current = context
        for part in match.group(1).split('.'):
            if not isinstance(current, dict) or part not in current:
                raise ValueError('Unknown action template field')
            current = current[part]
        return current
    match = TEMPLATE.fullmatch(value)
    if match:
        return lookup(match)
    def substitute(match):
        current = lookup(match)
        return current if isinstance(current, str) else json.dumps(current)
    return TEMPLATE.sub(substitute, value)


def selected_actions(scene, status):
    return [step.action_id for step in selected_steps(scene, status)]


def selected_steps(scene, status):
    steps, explicit_seen = [], set()
    for trigger in ([status] if status == 'normal' else [status, 'not_normal']):
        rule = getattr(scene.on, trigger)
        if isinstance(rule, ActionInstructions):
            resolved = rule._steps_by_status.get(status, rule._steps)
            if resolved is None:
                raise ValueError('Action instructions have not been resolved')
            if rule._explicit_order is not None:
                for step in resolved:
                    if step.action_id not in explicit_seen:
                        steps.append(step)
                        explicit_seen.add(step.action_id)
            else:
                steps.extend(resolved)
        else:
            for action_id in rule:
                if action_id not in explicit_seen:
                    steps.append(ActionStep(action_id=action_id))
                    explicit_seen.add(action_id)
    return steps


async def execute(action_id, config, context):
    action = config.actions[action_id]
    server = config.servers[action.server]
    outcome = {'action_id': action_id, 'name': action.name,
               'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running'}
    stage = 'argument templates'
    try:
        arguments = render(action.arguments, context)
        stage = 'MCP server startup'
        params = StdioServerParameters(
            command=sys.executable if server.command == 'python' else server.command,
            args=server.args, cwd=str(ROOT), env={**os.environ, **server.env})
        async with asyncio.timeout(server.timeout_seconds):
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    stage = 'MCP initialization'
                    await session.initialize()
                    stage = 'MCP tool discovery'
                    listing = await session.list_tools()
                    if action.tool not in {t.name for t in listing.tools}:
                        raise ValueError('Configured action tool was not found')
                    tool = next(t for t in listing.tools if t.name == action.tool)
                    stage = 'MCP argument validation'
                    validate(arguments, tool.inputSchema)
                    stage = 'MCP tool invocation'
                    result = await session.call_tool(action.tool, arguments)
                    if result.isError:
                        stage = 'MCP tool reported failure'
                        raise RuntimeError('Action tool reported failure')
                    stage = 'MCP session cleanup'
        outcome['status'] = 'succeeded'
    except asyncio.CancelledError:
        outcome.update(status='uncertain', error='Interrupted; action may have executed. Not retried.')
        raise
    except Exception as exc:
        # A lost response can follow a successful side effect. Never blindly retry.
        # Never log exception text, MCP error content, arguments, or tracebacks containing vendor data.
        logger.error('Action %s failed at %s (%s). Not retried.', action_id, stage, type(exc).__name__)
        outcome.update(status='failed', error_stage=stage,
                       error=f'Action failed at {stage}; delivery may be unconfirmed. See container logs. Not retried.')
    finally:
        outcome['finished_at'] = datetime.now(timezone.utc).isoformat()
    return outcome


def context_for(record):
    """Expose run-level results to action templates (legacy scene alias remains)."""
    lines = []
    for scene in record.get('scenes', []):
        lines.append(f"{scene['name']}: {scene['status']}")
        lines.extend(f"  {c['condition']}: {c['status']} — {c.get('explanation', '')}"
                     for c in scene.get('conditions', []))
    if not lines:
        lines = [f"{c['condition']}: {c['status']} — {c.get('explanation', '')}"
                 for c in record.get('conditions', [])]
    return {
        'scene': {'id': record.get('scene_id') or 'active_scenes', 'name': record['scene_name']},
        'run': {'id': record['id'], 'name': record['scene_name'],
                'scene_ids': record.get('scene_ids', []), 'status': record['status']},
        'result': {**{k: record.get(k) for k in ('status', 'summary', 'conditions', 'scenes')},
                   'details': '\n'.join(lines), 'plain_english': record.get('plain_english', '')},
        'check': {'id': record['id'], 'created_at': record['created_at']},
    }


def needs_report(value):
    if isinstance(value, dict):
        return any(needs_report(v) for v in value.values())
    if isinstance(value, list):
        return any(needs_report(v) for v in value)
    return isinstance(value, str) and any(m.group(1) == 'result.plain_english' for m in TEMPLATE.finditer(value))


def action_arguments(action, status, overrides):
    return {**action.arguments, **action.arguments_by_status.get(status, {}), **overrides}


async def run_actions(record, run, config, save, executor=execute, writer=write_report, *, steps=None):
    """Execute run-level actions; one generated report and at most one publish per run."""
    selected = selected_steps(run, record['status']) if steps is None else steps
    publishers = [step for step in selected if config.actions[step.action_id].tool == 'publish']
    if len(publishers) > 1:
        raise ValueError('A run may contain at most one publish notification action')
    record['actions'] = []
    # Synthesize the entire run once, before its actions, even if several use its report.
    report_steps = [step for step in selected if needs_report(
        action_arguments(config.actions[step.action_id], record['status'], step.arguments))]
    if report_steps:
        style = next((step.writing_instructions for step in report_steps if step.writing_instructions), '')
        record['progress'] = 'Writing plain-English run notification'
        save(record)
        try:
            record['plain_english'] = await writer({**record, 'writing_instructions': style})
            record['report_source'] = 'model'
        except Exception:
            logger.exception('Notification synthesis failed for run %s; using fallback', record['id'])
            record['plain_english'] = fallback_report(record)
            record['report_source'] = 'fallback'
        save(record)
    for step in selected:
        action_id = step.action_id
        action = config.actions[action_id]
        effective = config.model_copy(update={'actions': {**config.actions, action_id:
            action.model_copy(update={'arguments': action_arguments(action, record['status'], step.arguments)})}})
        pending = {'action_id': action_id, 'name': action.name,
                   'status': 'uncertain', 'error': 'Execution interrupted or unconfirmed; not retried.'}
        record['actions'].append(pending)
        save(record)
        # Mark only after saving intent succeeds: a storage failure before this
        # point must still allow an emergency notification. Once invocation starts,
        # even a lost response can mean delivery, so never send a second push.
        if action.tool == 'publish':
            record['notification_attempted'] = True
        outcome = await executor(action_id, effective, context_for(record))
        if needs_report(effective.actions[action_id].arguments):
            outcome.update(report=record['plain_english'], report_source=record['report_source'],
                           writing_instructions=step.writing_instructions)
        pending.clear()
        pending.update(outcome)
        save(record)
