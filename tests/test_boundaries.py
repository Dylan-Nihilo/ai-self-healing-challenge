import tempfile
import time
import unittest
import io
from unittest.mock import patch
from pathlib import Path
from fastapi.testclient import TestClient

from core.main import create_app
from core.workstation import WorkstationError


class WorkstationBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(Path(temporary.name), Path('/app/seed'), start_background=False)
        self.engine = self.app.state.engine
        self.controller = self.app.state.controller
        self.client = self.enterContext(TestClient(self.app, headers={'X-Workbench-Token': 'local-preview'}))

    def test_broken_functions_really_fail(self):
        self.assertEqual(self.client.get('/api/library').status_code, 503)
        self.assertEqual(self.client.get('/api/search', params={'q': '安全保存'}).status_code, 503)
        item = self.client.post('/api/jobs').json()
        while self.engine.process_next():
            pass
        self.assertEqual(self.engine.job(item['id'])['state'], 'failed')
        self.assertEqual(self.client.get(f"/api/jobs/{item['id']}/file").status_code, 409)
        self.assertFalse(self.client.get('/api/status').json()['ready'])

    def test_health_endpoint_is_liveness_only(self):
        self.assertEqual(self.client.get('/health').json()['meaning'], 'maintenance_process_alive')
        self.assertFalse(self.client.get('/api/status').json()['ready'])

    def test_owner_authentication_is_required(self):
        response = self.client.post('/api/drill', json={}, headers={'X-Workbench-Token': 'wrong'})
        self.assertEqual(response.status_code, 403)

    def test_repair_token_cannot_use_owner_endpoints(self):
        self.controller.active = {'id': 'test-run', 'token': 'repair-only', 'remaining': 2, 'deadline': time.monotonic() + 10}
        response = self.client.post('/api/drill', json={}, headers={'X-Workbench-Token': '', 'X-Repair-Token': 'repair-only'})
        self.assertEqual(response.status_code, 403)

    def test_tool_credentials_are_scoped_revoked_and_budgeted(self):
        self.controller.active = {'id': 'test-run', 'token': 'repair-only', 'remaining': 1, 'deadline': time.monotonic() + 10}
        self.controller.call('test-run', 'repair-only', 'list_manuals', {})
        with self.assertRaises(WorkstationError) as error:
            self.controller.call('test-run', 'repair-only', 'list_manuals', {})
        self.assertEqual(error.exception.code, 'tool_budget')
        self.controller.active = None
        with self.assertRaises(WorkstationError) as error:
            self.controller.call('test-run', 'repair-only', 'list_manuals', {})
        self.assertEqual(error.exception.status, 403)

    def test_invalid_tool_arguments_are_controlled_errors(self):
        for name, arguments in [('read_resource', {'resource': ['bad']}), ('read_manual', {'document_id': '../settings'}),
                                ('reload_worker', {'expected_revision': True}), ('restore_catalog', {'snapshot_id': 'x'})]:
            with self.subTest(name=name), self.assertRaises(WorkstationError):
                self.engine.tool(name, arguments)

    def test_agent_claim_does_not_replace_independent_readiness(self):
        self.controller.agent.update(state='watching', summary='Everything is fixed')
        self.assertFalse(self.client.get('/api/status').json()['ready'])

    def test_large_request_is_rejected(self):
        response = self.client.post('/api/drill', content='x' * 200_001, headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 413)

    def test_blocked_report_stops_repeating_the_same_repair(self):
        checks = [{'id': 'catalog', 'label': '资料读取', 'ok': False, 'detail': '目录不可读'}]
        with patch('core.controller.build_opener') as opener, patch.object(self.engine, 'run_checks', return_value=checks):
            opener.return_value.open.return_value = io.BytesIO(b'{"summary":"Need more information","outcome":"blocked"}')
            self.controller.repair(checks)
        self.assertEqual(self.controller.blocked_signature, ('catalog',))
        self.assertEqual(self.controller.agent['state'], 'blocked')
        self.assertIsNone(self.controller.active)
        self.client.post('/api/repair')
        self.assertIsNone(self.controller.blocked_signature)
