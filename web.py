"""REST host, dashboard, and daily check scheduler. Run one worker."""
import asyncio
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse
from agent import evaluate, check_timeout
from planner import resolve_plans
from settings import ROOT, load_config, data_dir, ActionStep
from actions import run_actions, execute
from action_planner import resolve_action_plans, describe_action_plans
from service import CheckService, BusyError, PlanError
from scheduler import run_scheduler
from logging_setup import configure_logging

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    configure_logging()
    logger.info('Preparing sensor and action plans')
    app.state.config = load_config()
    app.state.config_error = None
    try:
        await resolve_plans(app.state.config)
        await resolve_action_plans(app.state.config)
        logger.info('Startup plans ready scenes=%s sensors=%s',
                    len(app.state.config.scenes), len(app.state.config.sensors))
    except Exception as exc:
        logger.exception('Startup instruction planning failed')
        app.state.config_error = str(exc) if type(exc) in (ValueError, RuntimeError) else "Could not interpret startup instructions"
    app.state.folder = data_dir() / 'checks'
    app.state.folder.mkdir(exist_ok=True)
    app.state.active = None
    app.state.task = None
    app.state.token = secrets.token_urlsafe(32)
    records = []
    for path in app.state.folder.glob('*.json'):
        record = json.loads(path.read_text())
        if record['state'] == 'running':
            record.update(state='finished', status='unknown', summary='Check interrupted by restart')
            save(record)
        records.append(record)
    app.state.service = CheckService(app.state, save, run_check, records, on_failure=notify_check_failure)
    stop = asyncio.Event()
    scheduler_task = asyncio.create_task(run_scheduler(app.state.service, stop))
    try:
        yield
    finally:
        stop.set()
        await scheduler_task
        for task in list(app.state.service.failure_tasks):
            task.cancel()
        if app.state.service.failure_tasks:
            await asyncio.gather(*app.state.service.failure_tasks, return_exceptions=True)
        if app.state.task and not app.state.task.done():
            app.state.task.cancel()
            try:
                await app.state.task
            except asyncio.CancelledError:
                pass

app = FastAPI(title='Scene Watch', lifespan=lifespan)

@app.exception_handler(Exception)
async def api_failure(request, exc):
    error_id = uuid4().hex[:12]
    logger.error('API failure %s: %s %s', error_id, request.method, request.url.path,
                 exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=500,
                        content={'detail': f'Server error during {request.method} {request.url.path}. '
                                 f'Check container logs (error {error_id}).'},
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def save(record):
    path = app.state.folder / (record['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(record, indent=2))
        temporary.replace(path)
    except Exception:
        logger.exception('Check storage write failed for %s', record['id'])
        raise

async def notify_check_failure(record, scene_ids):
    """Emergency alert is a single run-level action path, without disk/model dependencies."""
    if record.get('failure_alerted') or record.get('notification_attempted'):
        return
    record['failure_alerted'] = True
    actions = app.state.config.run.on.failure
    if not actions:
        logger.error('Run %s failed; no run-level failure actions configured', record['id'])
        return
    labels = ', '.join(app.state.config.scenes[sid].name for sid in scene_ids)
    message = f"Could not complete the check for {labels}. Please check manually."
    alert = {**record, 'status': 'unknown', 'summary': message,
             'conditions': [], 'scenes': [], 'scene_name': labels}
    async def report(_):
        return message
    try:
        await run_actions(alert, app.state.config.run, app.state.config, lambda _: None,
                          writer=report,
                          steps=[ActionStep(action_id=action_id) for action_id in actions])
    except Exception:
        logger.exception('Failure notification could not complete for run %s', record['id'])


def aggregate_run(results):
    if not results:
        return 'unknown', 'No scenes were evaluated.'
    statuses = [result['status'] for result in results]
    status = ('needs_attention' if 'needs_attention' in statuses else
              'unknown' if 'unknown' in statuses else 'normal')
    ok = sum(status == 'normal' for status in statuses)
    return status, f'{ok}/{len(results)} scenes normal.'


async def run_check(record, scene_ids):
    started = time.monotonic()
    logger.info('Run started run=%s source=%s scenes=%s',
                record['id'], record.get('source', 'manual'), ','.join(scene_ids))
    failed = False
    def progress(message, trace, evidence):
        record.update(progress=message, trace=list(trace), evidence=list(evidence))
        save(record)
    try:
        all_trace, all_evidence = [], []
        for scene_id in scene_ids:
            scene = app.state.config.scenes[scene_id]
            logger.info('Scene started run=%s scene=%s name=%s', record['id'], scene_id, scene.name)
            progress(f'Checking {scene.name}', all_trace, all_evidence)
            def scene_progress(message, trace, evidence):
                progress(f'{scene.name}: {message}', all_trace + list(trace),
                         all_evidence + list(evidence))
            try:
                async with asyncio.timeout(check_timeout(scene, app.state.config)):
                    result = await evaluate(scene, app.state.config, scene_progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                logger.exception('Scene %s could not complete in run %s', scene_id, record['id'])
                result = {'status': 'unknown', 'summary': 'Scene evaluation could not complete',
                          'conditions': [{'condition': condition, 'status': 'unknown',
                                          'explanation': 'Evaluation failed; see server logs.',
                                          'evidence_ids': []} for condition in scene.conditions],
                          'sensors': [], 'evidence': [], 'trace': [],
                          'error_type': type(exc).__name__}
            record['scenes'].append({'scene_id': scene_id, 'name': scene.name, **result})
            logger.info('Scene finished run=%s scene=%s status=%s', record['id'], scene_id, result['status'])
            all_trace.extend(result.get('trace', []))
            all_evidence.extend(result.get('evidence', []))
            record['trace'], record['evidence'] = all_trace, all_evidence
            save(record)
        record['status'], record['summary'] = aggregate_run(record['scenes'])
        save(record)
    except asyncio.CancelledError:
        record.update(status='unknown', summary='Run interrupted')
        raise
    except Exception:
        failed = True
        logger.exception('Run %s failed', record['id'])
        record.update(status='unknown', summary='Run could not complete; check server logs')
    finally:
        try:
            if not asyncio.current_task().cancelling():
                try:
                    if failed:
                        await notify_check_failure(record, scene_ids)
                    else:
                        await run_actions(record, app.state.config.run, app.state.config, save)
                except Exception:
                    logger.exception('Action processing failed for run %s', record['id'])
                    record['action_error'] = 'Action processing could not complete; not retried.'
                    await notify_check_failure(record, scene_ids)
        finally:
            record.update(state='finished', finished_at=datetime.now(timezone.utc).isoformat())
            try:
                save(record)
            except Exception:
                if not failed and not asyncio.current_task().cancelling():
                    await notify_check_failure(record, scene_ids)
                raise
            finally:
                logger.info('Run finished run=%s status=%s actions=%s elapsed=%.1fs',
                            record['id'], record.get('status', 'unknown'),
                            ','.join(a.get('status', 'unknown') for a in record.get('actions', [])) or 'none',
                            time.monotonic() - started)
                app.state.active = None

@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html')

@app.get('/api/scenes')
def scenes():
    return {'scenes': {k: v.model_dump() for k, v in app.state.config.scenes.items()},
            'run': {**app.state.config.run.model_dump(), 'action_plans': describe_action_plans(app.state.config.run)},
            'sensors': {k: {'name': v.name} for k, v in app.state.config.sensors.items()},
            'actions': {k: {'name': v.name, 'manual': v.manual} for k, v in app.state.config.actions.items()},
            'schedules': {k: v.model_dump() for k, v in app.state.config.schedules.items()},
            'token': app.state.token,
            'active_check': app.state.active, 'config_error': app.state.config_error}

@app.post('/api/checks/active', status_code=202)
async def start_active(x_check_token: str | None = Header(default=None)):
    if app.state.config.actions and x_check_token != app.state.token:
        raise HTTPException(403, 'Refresh the dashboard and try again')
    try:
        return app.state.service.submit()
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except BusyError as exc:
        raise HTTPException(409, str(exc)) from None

@app.post('/api/scenes/{scene_id}/checks', status_code=202)
async def start(scene_id: str, x_check_token: str | None = Header(default=None)):
    if app.state.config.actions and x_check_token != app.state.token:
        raise HTTPException(403, 'Refresh the dashboard and try again')
    try:
        return app.state.service.submit(scene_id)
    except PlanError as exc:
        raise HTTPException(422, str(exc)) from None
    except KeyError:
        raise HTTPException(404, 'Scene not found') from None
    except BusyError as exc:
        raise HTTPException(409, str(exc)) from None

async def run_manual(record, action_id):
    try:
        record['actions'] = [await execute(action_id, app.state.config, {})]
        succeeded = record['actions'][0]['status'] == 'succeeded'
        record.update(status='normal' if succeeded else 'unknown', summary='Action completed' if succeeded else 'Action unconfirmed')
    except Exception:
        logger.exception('Manual action %s failed for check %s', action_id, record['id'])
        record.update(status='unknown', summary='Action failed; check container logs')
    finally:
        record.update(state='finished', finished_at=datetime.now(timezone.utc).isoformat())
        try:
            save(record)
        finally:
            app.state.active = None

@app.post('/api/actions/{action_id}/runs', status_code=202)
async def manual_action(action_id: str, x_check_token: str | None = Header(default=None)):
    if x_check_token != app.state.token:
        raise HTTPException(403, 'Refresh the dashboard and try again')
    action = app.state.config.actions.get(action_id)
    if action is None or not action.manual:
        raise HTTPException(404, 'Manual action not found')
    if app.state.active:
        raise HTTPException(409, 'A check or action is already running')
    record = {'id': uuid4().hex, 'scene_id': '', 'scene_name': action.name, 'kind': 'action', 'source': 'manual',
              'created_at': datetime.now(timezone.utc).isoformat(), 'state': 'running',
              'status': 'unknown', 'summary': 'Running action', 'conditions': [], 'evidence': [], 'trace': [],
              'actions': [{'action_id': action_id, 'name': action.name, 'status': 'uncertain',
                           'error': 'Execution interrupted or unconfirmed; not retried.'}]}
    save(record)
    app.state.active = record['id']
    app.state.task = asyncio.create_task(run_manual(record, action_id))
    return record

@app.get('/api/checks')
def checks():
    records = [json.loads(p.read_text()) for p in app.state.folder.glob('*.json')]
    return sorted(records, key=lambda r: r['created_at'], reverse=True)[:50]

@app.get('/api/checks/{check_id}')
def check(check_id: str):
    if len(check_id) != 32 or any(c not in '0123456789abcdef' for c in check_id):
        raise HTTPException(404)
    path = app.state.folder / (check_id + '.json')
    if not path.exists():
        raise HTTPException(404)
    return json.loads(path.read_text())

@app.get('/frames/{filename}')
def frame(filename: str):
    stem, _, extension = filename.rpartition('.')
    if extension not in ('jpg','png','webp') or len(stem) != 32 or any(c not in '0123456789abcdef' for c in stem):
        raise HTTPException(404)
    path = data_dir() / 'frames' / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ['PORT']))
