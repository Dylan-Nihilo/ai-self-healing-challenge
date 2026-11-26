import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from shared.contracts import ToolCall
from .controller import Controller
from .workstation import Workstation, WorkstationError
from .body_limit import BodyLimit


class FaultInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(default='random', max_length=30)


def create_app(data=None, seed=None, start_background=True):
    engine = Workstation(Path(data or os.environ.get('WORKSTATION_DATA', '/data')), Path(seed or os.environ.get('SEED_DIR', '/app/seed')))
    controller = Controller(engine, os.environ.get('RUNNER_URL', 'http://runner:8200'), os.environ.get('TOOL_API_URL', 'http://core:8000'))
    owner = os.environ.get('WORKBENCH_TOKEN', 'local-preview')

    @asynccontextmanager
    async def lifespan(app):
        if start_background:
            controller.start()
        try:
            yield
        finally:
            controller.close()

    app = FastAPI(title='Self-healing document workstation', lifespan=lifespan)
    app.state.engine, app.state.controller = engine, controller
    app.add_middleware(BodyLimit, max_bytes=200_000)

    @app.middleware('http')
    async def authorize(request: Request, call_next):
        if request.url.path.startswith('/api/') and not secrets.compare_digest(request.headers.get('x-workbench-token', ''), owner):
            return JSONResponse({'error': {'code': 'forbidden', 'message': '无权访问工作站。'}}, status_code=403)
        return await call_next(request)

    @app.exception_handler(WorkstationError)
    async def failed(request, error):
        return JSONResponse({'error': {'code': error.code, 'message': error.message}}, status_code=error.status)

    @app.get('/health')
    def health():
        return {'status': 'ok', 'meaning': 'maintenance_process_alive'}

    @app.get('/api/status')
    def status():
        checks = engine.checks
        return {'version': '2.1', 'ready': bool(checks) and all(item['ok'] for item in checks),
                'checks': checks, 'checked_at': engine.checked_at, 'agent': dict(controller.agent),
                'events': engine.events(), 'jobs': engine.jobs()[:15], 'monitor_interval_seconds': 5}

    @app.post('/api/checks')
    def check():
        return {'checks': engine.run_checks()}

    @app.post('/api/repair')
    def retry():
        controller.blocked_signature = None
        controller.last_attempt = 0
        controller.wake.set()
        return {'scheduled': True}

    @app.post('/api/drill')
    def drill(body: FaultInput):
        if controller.active:
            raise WorkstationError('repair_running', '请等待当前修复完成后再注入新故障。')
        kind = secrets.choice(['catalog', 'search', 'worker']) if body.kind == 'random' else body.kind
        engine.inject(kind)
        engine.checks = []
        engine.checked_at = None
        controller.blocked_signature = None
        controller.last_attempt = 0
        controller.wake.set()
        return {'scheduled': True}

    @app.get('/api/library')
    def library():
        return engine.library()

    @app.get('/api/documents/{identifier}')
    def document(identifier: str):
        return engine.document(identifier)

    @app.get('/api/search')
    def search(q: str = ''):
        if not 1 <= len(q.strip()) <= 100:
            raise WorkstationError('invalid_query', '请输入 1–100 字的检索内容。', 400)
        return engine.search(q.strip())

    @app.post('/api/jobs', status_code=201)
    def create_job():
        return engine.create_job()

    @app.get('/api/jobs/{identifier}/file')
    def download(identifier: str):
        return FileResponse(engine.artifact(identifier), media_type='application/zip', filename='资料归档.zip')

    @app.post('/internal/runs/{run_id}/tools')
    def tool(run_id: str, body: ToolCall, request: Request):
        return {'data': controller.call(run_id, request.headers.get('x-repair-token', ''), body.name, body.arguments)}

    return app
