import json
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from shared.contracts import RepairResult
from .workstation import WorkstationError


class Controller:
    def __init__(self, engine, runner_url, tool_url):
        self.engine, self.runner_url, self.tool_url = engine, runner_url, tool_url
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.active = None
        self.last_attempt = 0
        self.agent = {'state': 'waiting', 'message': '正在检查自身功能。', 'last_run_at': None, 'summary': None}
        self.last_problem = None
        self.threads = []
        self.repair_thread = None
        self.blocked_signature = None

    def start(self):
        for target in [self.work, self.monitor]:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            self.active = None
        for thread in self.threads + ([self.repair_thread] if self.repair_thread else []):
            thread.join(timeout=3)

    def work(self):
        while not self.stop.is_set():
            try:
                self.engine.process_next()
            except Exception:
                self.engine.event('system', '归档工作进程发生异常，等待下一次检查。')
            self.stop.wait(.1)

    def monitor(self):
        while not self.stop.is_set():
            try:
                checks = self.engine.run_checks()
                healthy = all(item['ok'] for item in checks)
                signature = tuple(item['id'] for item in checks if not item['ok'])
                if healthy and not self.active:
                    if self.agent['state'] != 'watching':
                        self.engine.event('checker', '全部功能核验通过，继续监测。', {'passed': len(checks)})
                    self.agent.update(state='watching', message='各项功能正常，持续自动自检。')
                    self.last_problem = None
                    self.blocked_signature = None
                elif not healthy and not self.active and not (self.repair_thread and self.repair_thread.is_alive()) and signature != self.blocked_signature and time.monotonic() - self.last_attempt >= 15:
                    self.last_attempt = time.monotonic()
                    self.repair_thread = threading.Thread(target=self.repair, args=(checks,), daemon=True)
                    self.repair_thread.start()
            except Exception:
                self.agent.update(state='error', message='自检未能完成，系统将再次尝试。')
            self.wake.wait(5)
            self.wake.clear()

    def repair(self, checks):
        self.last_attempt = time.monotonic()
        with self.lock:
            run_id, token = str(uuid4()), secrets.token_urlsafe(32)
            self.active = {'id': run_id, 'token': token, 'remaining': 32, 'deadline': time.monotonic() + 125}
        self.agent.update(state='running', message='修复模块正在调查自身故障。', last_run_at=self.engine.checked_at)
        blocked = False
        body = {'context': {'system_version': '2.1', 'checks': checks, 'previous_summary': self.agent['summary']},
                'run_id': run_id, 'tool_token': token, 'tool_api_url': self.tool_url}
        request = Request(self.runner_url + '/repair', json.dumps(body).encode(), {'Content-Type': 'application/json'}, method='POST')
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=130) as response:
                result = RepairResult.model_validate(json.load(response))
            self.agent.update(state='verifying', message='修复模块已返回，正在独立核验。', summary=result.summary)
            self.engine.event('assistant', '修复模块返回处理说明', result.model_dump())
            blocked = result.outcome == 'blocked'
            self.last_problem = None
        except HTTPError as error:
            try:
                detail = json.load(error)['error']
            except (ValueError, KeyError):
                detail = {'code': 'runner_error', 'message': '修复模块返回了无法识别的响应。'}
            state = 'not_connected' if detail['code'] == 'solution_not_implemented' else 'error'
            self.agent.update(state=state, message=detail['message'])
            if self.last_problem != detail['code']:
                self.engine.event('system', detail['message'], {'code': detail['code']})
            self.last_problem = detail['code']
        except Exception:
            self.agent.update(state='error', message='暂时无法完成本轮修复，系统将继续自检。')
            if self.last_problem != 'runner_unavailable':
                self.engine.event('system', '修复模块调用失败，原始资料与任务已保留。')
            self.last_problem = 'runner_unavailable'
        finally:
            with self.lock:
                self.active = None
        checked = self.engine.run_checks()
        if blocked and not all(item['ok'] for item in checked):
            self.blocked_signature = tuple(item['id'] for item in checked if not item['ok'])
            self.agent.update(state='blocked', message='修复模块需要补充处理，自检继续运行。更新实现后可重新运行。')

    def call(self, run_id, token, name, arguments):
        with self.lock:
            current = self.active
            if not current or run_id != current['id'] or not secrets.compare_digest(token, current['token']) or time.monotonic() > current['deadline']:
                raise WorkstationError('invalid_run', '本次修复凭据已失效。', 403)
            if current['remaining'] <= 0:
                raise WorkstationError('tool_budget', '本轮工具预算已用完。', 429)
            current['remaining'] -= 1
            # Keep authorization valid through each mutation; a revoked run cannot race a write.
            try:
                value = self.engine.tool(name, arguments)
                recorded = {'event_count': len(value['events'])} if name == 'read_resource' and arguments.get('resource') == 'logs' else value
                self.engine.event('assistant', '调用 ' + name, {'arguments': arguments, 'result': recorded})
                return value
            except WorkstationError as error:
                self.engine.event('assistant', '工具调用未完成', {'tool': name, 'code': error.code, 'message': error.message})
                raise
