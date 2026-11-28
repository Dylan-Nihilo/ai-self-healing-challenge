import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from core.workstation import Workstation
from shared.contracts import RepairContext
from shared.tools import ToolError
from solution.agent import run_repair
from solution.model_client import ModelSettings, OpenAICompatibleModel

ROOT = Path(__file__).resolve().parents[2]


class DirectTools:
    def __init__(self, engine):
        self.engine = engine
        self.calls = []

    def call(self, name, **arguments):
        self.calls.append((name, arguments))
        try:
            return {"data": self.engine.tool(name, arguments)}
        except Exception as error:
            raise ToolError(getattr(error, "code", "tool_error"), str(error)) from error


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def complete(self, messages, tools):
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("The repair loop requested more model turns than scripted")
        return self.responses.pop(0)


def response(*calls):
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        tool_calls.append(
            {
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


class RepairAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = Workstation(Path(self.temporary.name), ROOT / "seed")
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()
        self.addCleanup(self._stop_worker)
        self._wait_for_jobs()

    def _work(self):
        while not self.stop.is_set():
            self.engine.process_next()
            time.sleep(0.01)

    def _stop_worker(self):
        self.stop.set()
        self.worker.join(timeout=2)

    def _wait_for_jobs(self, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(job["state"] not in {"queued", "working"} for job in self.engine.jobs()):
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for the archive worker")

    def _context(self):
        checks = self.engine.run_checks()
        return RepairContext(system_version="2.1", checks=checks)

    def _failed_jobs(self):
        return [job["id"] for job in self.engine.jobs() if job["state"] == "failed"]

    def _v2_snapshot(self):
        return next(item for item in self.engine.meta("snapshots") if item["version"] == "2.x")

    def test_initial_mixed_repair_and_each_supported_repeat_fault(self):
        catalog_sha = self.engine.tool("read_resource", {"resource": "catalog"})["sha256"]
        snapshot = self._v2_snapshot()
        settings = self.engine.settings()
        storage = next(item["id"] for item in self.engine.tool("inspect_storage", {})["directories"] if item["available"])
        retries = [("retry_job", {"job_id": job_id}) for job_id in self._failed_jobs()]
        first = ScriptedModel(
            [
                response(
                    ("read_snapshot", {"snapshot_id": snapshot["id"]}),
                    ("restore_catalog", {"snapshot_id": snapshot["id"], "expected_sha256": catalog_sha}),
                ),
                response(
                    ("rebuild_index", {"expected_catalog_sha": snapshot["sha256"]}),
                    ("update_settings", {"expected_revision": settings["revision"], "export_directory": storage}),
                ),
                response(
                    ("reload_worker", {"expected_revision": settings["revision"] + 1}),
                    *retries,
                ),
                response(("finish_repair", {"summary": "Restored dynamic workstation resources and retained jobs.", "outcome": "changed"})),
            ]
        )
        result = run_repair(self._context(), DirectTools(self.engine), model=first)
        self.assertEqual(result.outcome, "changed", result.summary)
        self.assertTrue(all(item["ok"] for item in self.engine.run_checks()))

        self.engine.inject("search")
        self._wait_for_jobs()
        next_catalog_sha = self.engine.tool("read_resource", {"resource": "catalog"})["sha256"]
        retries = [("retry_job", {"job_id": job_id}) for job_id in self._failed_jobs()]
        second = ScriptedModel(
            [
                response(("rebuild_index", {"expected_catalog_sha": next_catalog_sha}), *retries),
                response(("finish_repair", {"summary": "Rebuilt the search index and retried the preserved failed job.", "outcome": "changed"})),
            ]
        )
        result = run_repair(self._context(), DirectTools(self.engine), model=second)
        self.assertEqual(result.outcome, "changed", result.summary)
        self.assertTrue(all(item["ok"] for item in self.engine.run_checks()))

        self.engine.inject("catalog")
        self._wait_for_jobs()
        catalog_sha = self.engine.tool("read_resource", {"resource": "catalog"})["sha256"]
        snapshot = self._v2_snapshot()
        retries = [("retry_job", {"job_id": job_id}) for job_id in self._failed_jobs()]
        catalog_model = ScriptedModel(
            [
                response(
                    ("read_snapshot", {"snapshot_id": snapshot["id"]}),
                    ("restore_catalog", {"snapshot_id": snapshot["id"], "expected_sha256": catalog_sha}),
                ),
                response(
                    ("rebuild_index", {"expected_catalog_sha": snapshot["sha256"]}),
                    *retries,
                ),
                response(("finish_repair", {"summary": "Recovered the catalog from the verified v2 snapshot.", "outcome": "changed"})),
            ]
        )
        result = run_repair(self._context(), DirectTools(self.engine), model=catalog_model)
        self.assertEqual(result.outcome, "changed", result.summary)
        self.assertTrue(all(item["ok"] for item in self.engine.run_checks()))

        self.engine.inject("worker")
        self._wait_for_jobs()
        settings = self.engine.settings()
        storage = next(item["id"] for item in self.engine.tool("inspect_storage", {})["directories"] if item["available"])
        retries = [("retry_job", {"job_id": job_id}) for job_id in self._failed_jobs()]
        worker_model = ScriptedModel(
            [
                response(
                    ("update_settings", {"expected_revision": settings["revision"], "export_directory": storage}),
                ),
                response(
                    ("reload_worker", {"expected_revision": settings["revision"] + 1}),
                    *retries,
                ),
                response(("finish_repair", {"summary": "Reloaded the worker with an available archive destination.", "outcome": "changed"})),
            ]
        )
        result = run_repair(self._context(), DirectTools(self.engine), model=worker_model)
        self.assertEqual(result.outcome, "changed", result.summary)
        self.assertTrue(all(item["ok"] for item in self.engine.run_checks()))

    def test_model_receives_version_matched_manual_and_dynamic_ids(self):
        snapshot = self._v2_snapshot()
        model = ScriptedModel(
            [
                response(
                    ("finish_repair", {"summary": "Need a wider repair after observing the real manual.", "outcome": "blocked"})
                )
            ]
        )
        tools = DirectTools(self.engine)
        result = run_repair(self._context(), tools, model=model)
        self.assertEqual(result.outcome, "blocked")
        evidence_prompt = model.messages[0][1]["content"]
        self.assertIn("工作站 2.x 运行与恢复手册", evidence_prompt)
        self.assertIn(snapshot["id"], evidence_prompt)
        self.assertNotIn("MODEL_API_KEY", evidence_prompt)

    def test_tool_error_is_returned_to_model_for_correction(self):
        snapshot = next(item for item in self.engine.meta("snapshots") if item["version"] == "2.x")
        model = ScriptedModel(
            [
                response(("read_snapshot", {"snapshot_id": "invented-id"})),
                response(("read_snapshot", {"snapshot_id": snapshot["id"]})),
                response(("finish_repair", {"summary": "Verified the actual snapshot identifier.", "outcome": "blocked"})),
            ]
        )
        result = run_repair(self._context(), DirectTools(self.engine), model=model)
        self.assertEqual(result.outcome, "blocked")
        second_turn = model.messages[1]
        tool_messages = [message for message in second_turn if message.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertIn("snapshot_missing", tool_messages[0]["content"])

    def test_missing_model_configuration_is_blocked_without_deterministic_repair(self):
        keys = ["MODEL_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "MODEL_NAME"]
        with patch.dict("os.environ", {key: "" for key in keys}):
            result = run_repair(self._context(), DirectTools(self.engine), model=None)
        self.assertEqual(result.outcome, "blocked")
        self.assertIn("Model not configured", result.summary)
        self.assertFalse(all(item["ok"] for item in self.engine.run_checks()))

    def test_openai_compatible_transport_returns_tool_call(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received["body"] = json.loads(self.rfile.read(length))
                payload = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "finish_repair",
                                            "arguments": (
                                                '{"summary":"blocked","outcome":"blocked"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        model = OpenAICompatibleModel(
            ModelSettings(
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                api_key="test-key",
                model="test-model",
                timeout_seconds=5,
                max_retries=0,
            )
        )
        message = model.complete([{"role": "user", "content": "test"}], [])
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "finish_repair")
        self.assertEqual(received["body"]["model"], "test-model")
        self.assertEqual(received["body"]["tool_choice"], "auto")


if __name__ == "__main__":
    unittest.main()
