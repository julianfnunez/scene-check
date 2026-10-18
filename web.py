"""REST host, dashboard, and daily check scheduler. Run one worker."""
import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from agent import evaluate, check_timeout
from planner import resolve_plans
from settings import ROOT, load_config, data_dir
from actions import run_actions, execute
from action_planner import resolve_action_plans, describe_action_plans
from service import CheckService, BusyError, PlanError
from scheduler import run_scheduler

@asynccontextmanager
async def lifespan(app):
    app.state.config = load_config()
    app.state.config_error = None
    try:
        await resolve_plans(app.state.config)
        await resolve_action_plans(app.state.config)
    except Exception as exc:
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
    app.state.service = CheckService(app.state, save, run_check, records)
    stop = asyncio.Event()
    scheduler_task = asyncio.create_task(run_scheduler(app.state.service, stop))
    try:
        yield
    finally:
        stop.set()
        await scheduler_task
        if app.state.task and not app.state.task.done():
            app.state.task.cancel()
            try:
                await app.state.task
            except asyncio.CancelledError:
                pass

app = FastAPI(title='Scene Watch', lifespan=lifespan)

@app.middleware('http')
async def response_headers(request, call_next):
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response

def save(record):
    path = app.state.folder / (record['id'] + '.json')
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=2))
    temporary.replace(path)

async def run_check(record, scene):
    def progress(message, trace, evidence):
        record.update(progress=message, trace=list(trace), evidence=list(evidence))
        save(record)
    try:
        timeout = check_timeout(scene, app.state.config)
        async with asyncio.timeout(timeout):
            result = await evaluate(scene, app.state.config, progress)
        record.update(result)
    except asyncio.CancelledError:
        record.update(status='unknown', summary='Check interrupted')
        raise
    except Exception as exc:
        # Never expose vendor responses, camera URLs, or exception groups containing secrets.
        message = str(exc) if type(exc) is RuntimeError else f'Check could not complete ({type(exc).__name__})'
        record.update(status='unknown', summary=message)
    finally:
        try:
            if not asyncio.current_task().cancelling():
                try:
                    await run_actions(record, scene, app.state.config, save)
                except Exception:
                    record['action_error'] = 'Action processing could not complete; not retried.'
        finally:
            record.update(state='finished', finished_at=datetime.now(timezone.utc).isoformat())
            save(record)
            app.state.active = None

@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html')

@app.get('/api/scenes')
def scenes():
    return {'scenes': {k: {**v.model_dump(), 'action_plans': describe_action_plans(v)} for k, v in app.state.config.scenes.items()},
            'sensors': {k: {'name': v.name} for k, v in app.state.config.sensors.items()},
            'actions': {k: {'name': v.name, 'manual': v.manual} for k, v in app.state.config.actions.items()},
            'schedules': {k: v.model_dump() for k, v in app.state.config.schedules.items()},
            'token': app.state.token,
            'active_check': app.state.active, 'config_error': app.state.config_error}

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
    finally:
        record.update(state='finished', finished_at=datetime.now(timezone.utc).isoformat())
        save(record)
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
