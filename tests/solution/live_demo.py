"""Run the real model against an isolated seeded workstation.

Usage (inside the repository root):
    python -m tests.solution.live_demo

The script requires the same .env.models values documented in docs/DELIVERY.md.
It never resets the Compose data volume and never prints credentials.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from core.workstation import Workstation
from shared.contracts import RepairContext
from shared.tools import ToolError
from solution.agent import repair

ROOT = Path(__file__).resolve().parents[2]


def load_model_environment():
    path = ROOT / ".env.models"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class ObservedTools:
    def __init__(self, engine: Workstation):
        self.engine = engine
        self.calls: list[dict] = []

    def call(self, name, **arguments):
        try:
            value = {"data": self.engine.tool(name, arguments)}
            self.calls.append({"name": name, "arguments": arguments, "ok": True})
            return value
        except Exception as error:
            self.calls.append(
                {
                    "name": name,
                    "arguments": arguments,
                    "ok": False,
                    "code": getattr(error, "code", "tool_error"),
                }
            )
            raise ToolError(
                getattr(error, "code", "tool_error"), str(error)
            ) from error


def wait_for_worker(engine: Workstation, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(job["state"] not in {"queued", "working"} for job in engine.jobs()):
            return
        time.sleep(0.03)
    raise RuntimeError("Archive worker did not settle")


def work_forever(engine: Workstation, stop: threading.Event):
    while not stop.is_set():
        engine.process_next()
        time.sleep(0.01)


def run_round(engine: Workstation, label: str):
    wait_for_worker(engine)
    checks = engine.run_checks()
    context = RepairContext(system_version="2.1", checks=checks)
    tools = ObservedTools(engine)
    result = repair(context, tools)
    final_checks = engine.run_checks()
    record = {
        "scenario": label,
        "initial_checks": checks,
        "tool_calls": tools.calls,
        "repair_result": result.model_dump(),
        "final_checks": final_checks,
    }
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if result.outcome != "changed" or not all(item["ok"] for item in final_checks):
        raise SystemExit(f"Scenario failed: {label}")


def main():
    load_model_environment()
    with tempfile.TemporaryDirectory() as temporary:
        engine = Workstation(Path(temporary), ROOT / "seed")
        stop = threading.Event()
        thread = threading.Thread(target=work_forever, args=(engine, stop), daemon=True)
        thread.start()
        try:
            run_round(engine, "initial-mixed-fault")
            for kind in ("catalog", "search", "worker"):
                engine.inject(kind)
                run_round(engine, f"repeat-{kind}")
        finally:
            stop.set()
            thread.join(timeout=2)


if __name__ == "__main__":
    main()
