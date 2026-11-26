import json
import os
import signal
import subprocess
import sys
import threading
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from shared.contracts import RepairResult, RunnerRequest


def create_app():
    app = FastAPI(title='Workstation repair worker')
    slot = threading.BoundedSemaphore(1)

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/repair', response_model=RepairResult)
    def repair(body: RunnerRequest):
        if not slot.acquire(blocking=False):
            return JSONResponse({'error': {'code': 'runner_busy', 'message': '修复模块正在执行。'}}, status_code=429)
        try:
            process = subprocess.Popen([sys.executable, '-m', 'runner.execute'], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                output, _ = process.communicate(body.model_dump_json().encode(), timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                return JSONResponse({'error': {'code': 'repair_timeout', 'message': '本轮修复已超时，运行已终止。'}}, status_code=504)
            if process.returncode or len(output) > 1_000_000:
                raise ValueError('Invalid repair output')
            data = json.loads(output)
            if data.get('error') == 'solution_not_implemented':
                return JSONResponse({'error': {'code': 'solution_not_implemented', 'message': '修复模块尚未实现。'}}, status_code=501)
            if 'error' in data:
                raise ValueError('Repair failed')
            return RepairResult.model_validate(data['result'])
        except Exception:
            return JSONResponse({'error': {'code': 'repair_failed', 'message': '修复模块执行失败，请检查实现与模型配置。'}}, status_code=500)
        finally:
            slot.release()

    return app
