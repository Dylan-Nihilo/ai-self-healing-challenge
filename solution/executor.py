"""Bounded, evidence-producing execution of repair tool scripts."""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from shared.tools import ToolClient, ToolError

FATAL_CODES = {"tool_budget", "invalid_run", "probe_owned_by_checker", "snapshot_corrupted"}
STEP_CODES = {"resource_changed", "revision_conflict", "invalid_tool_arguments", "snapshot_missing", "manual_missing", "storage_unavailable"}
GUARDED_TOOLS = {"restore_catalog", "rebuild_index", "update_settings", "reload_worker"}
MAX_SELF_CHECKS = 2

@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    why: str = ""

@dataclass
class FailureRecord:
    tool: str
    code: str
    message: str
    step_index: int
    severity: str

@dataclass
class ToolEvidence:
    index: int
    tool: str
    args: dict[str, Any]
    ok: bool
    data: Any = None
    error: FailureRecord | None = None

@dataclass
class ExecutionReport:
    evidence: list[ToolEvidence] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)
    calls_used: int = 0
    budget_exhausted: bool = False
    deadline_reached: bool = False
    stopped: bool = False

def failure_severity(code: str) -> str:
    return "step" if code in STEP_CODES else "fatal"

def _facts(report: ExecutionReport, tool: str, args: dict[str, Any], data: Any) -> None:
    if not isinstance(data, dict):
        return
    if tool == "read_resource":
        resource = args.get("resource")
        if (resource == "catalog" or "sha256" in data) and isinstance(data.get("sha256"), str):
            report.facts["catalog_sha"] = data["sha256"]
        if resource == "settings" and isinstance(data.get("revision"), int):
            report.facts["revision"] = data["revision"]
        if resource == "loaded_settings" and isinstance(data.get("revision"), int):
            report.facts["loaded_revision"] = data["revision"]
        if resource == "jobs" and isinstance(data.get("jobs"), list):
            report.facts["jobs"] = [{"id": j["id"], "state": j.get("state")} for j in data["jobs"] if isinstance(j, dict) and isinstance(j.get("id"), str)]
            report.facts["job_ids"] = [j["id"] for j in report.facts["jobs"] if j["state"] != "done"]
    elif tool == "restore_catalog":
        for key in ("sha256", "catalog_sha"):
            if isinstance(data.get(key), str):
                report.facts["restored_sha"] = data[key]
                break
    elif tool == "update_settings" and isinstance(data.get("revision"), int):
        report.facts["revision"] = data["revision"]
    elif tool == "inspect_storage" and isinstance(data.get("directories"), list):
        report.facts["storage_directories"] = data["directories"]

def _resolve(value: Any, report: ExecutionReport) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        key = value[1:]
        if key not in report.facts:
            raise ValueError(f"missing execution fact: {key}")
        return report.facts[key]
    if isinstance(value, dict):
        return {k: _resolve(v, report) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, report) for v in value]
    return value

def _validate(step: Step, args: dict[str, Any], report: ExecutionReport) -> None:
    if step.tool == "restore_catalog":
        sid = args.get("snapshot_id")
        observed = any(e.ok and e.tool in {"list_snapshots", "read_snapshot"} and sid in str(e.data) for e in report.evidence)
        if not observed:
            raise ValueError("snapshot_id was not observed in this run")
        if args.get("expected_sha256") != report.facts.get("catalog_sha"):
            raise ValueError("restore_catalog requires latest catalog_sha")
    elif step.tool == "rebuild_index" and args.get("expected_catalog_sha") != report.facts.get("restored_sha"):
        raise ValueError("rebuild_index requires latest restored_sha")
    elif step.tool in {"update_settings", "reload_worker"} and args.get("expected_revision") != report.facts.get("revision"):
        raise ValueError(f"{step.tool} requires latest revision")
    elif step.tool == "retry_job" and args.get("job_id") not in report.facts.get("job_ids", []):
        raise ValueError("retry_job job_id was not observed as pending in this run")

def execute(steps: Sequence[Step], tools: ToolClient, *, deadline: float, budget: int = 32, initial_facts: dict[str, Any] | None = None, initial_evidence: Sequence[ToolEvidence] = (), initial_calls: int = 0) -> ExecutionReport:
    report = ExecutionReport(evidence=list(initial_evidence), facts=dict(initial_facts or {}), calls_used=initial_calls)
    guards: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for index, step in enumerate(steps):
        args: dict[str, Any] = dict(step.args)
        if deadline - time.monotonic() < 20:
            report.deadline_reached = report.stopped = True
            break
        if report.calls_used >= budget:
            report.budget_exhausted = report.stopped = True
            report.failures.append(FailureRecord("<budget>", "tool_budget", "tool budget exhausted", index, "fatal"))
            break
        try:
            args = _resolve(step.args, report)
            _validate(step, args, report)
            if step.tool in GUARDED_TOOLS:
                key = (step.tool, tuple(sorted((k, str(v)) for k, v in args.items())))
                if key in guards:
                    continue
                guards.add(key)
            report.calls_used += 1
            data = tools.call(step.tool, **args).get("data")
            report.evidence.append(ToolEvidence(index, step.tool, args, True, data=data))
            _facts(report, step.tool, args, data)
            if step.tool == "run_self_check":
                report.facts["self_checks"] = report.facts.get("self_checks", 0) + 1
                if report.facts["self_checks"] >= MAX_SELF_CHECKS:
                    break
        except ToolError as error:
            severity = "step" if step.tool == "retry_job" else failure_severity(error.code)
            failure = FailureRecord(step.tool, error.code, str(error), index, severity)
            report.failures.append(failure)
            report.evidence.append(ToolEvidence(index, step.tool, args, False, error=failure))
            if severity == "fatal":
                report.stopped = True
                break
        except ValueError as error:
            failure = FailureRecord(step.tool, "invalid_tool_arguments", str(error), index, "fatal")
            report.failures.append(failure)
            report.evidence.append(ToolEvidence(index, step.tool, step.args, False, error=failure))
            report.stopped = True
            break
    return report

def build_catalog_script(snapshot_id: str) -> list[Step]:
    return [
        Step("read_resource", {"resource": "catalog"}, "capture current catalog hash"),
        Step("list_snapshots", {}, "inspect snapshot versions"),
        Step("read_snapshot", {"snapshot_id": snapshot_id}, "verify selected 2.x snapshot"),
        Step("restore_catalog", {"snapshot_id": snapshot_id, "expected_sha256": "$catalog_sha"}),
        Step("rebuild_index", {"expected_catalog_sha": "$restored_sha"}),
        Step("run_self_check"),
    ]

def build_mixed_script(snapshot_id: str, export_directory: str) -> list[Step]:
    return [
        Step("read_resource", {"resource": "catalog"}),
        Step("list_snapshots"),
        Step("read_snapshot", {"snapshot_id": snapshot_id}),
        Step("restore_catalog", {"snapshot_id": snapshot_id, "expected_sha256": "$catalog_sha"}),
        Step("rebuild_index", {"expected_catalog_sha": "$restored_sha"}),
        Step("read_resource", {"resource": "settings"}),
        Step("read_resource", {"resource": "loaded_settings"}),
        Step("inspect_storage"),
        Step("update_settings", {"expected_revision": "$revision", "export_directory": export_directory}),
        Step("reload_worker", {"expected_revision": "$revision"}),
        Step("read_resource", {"resource": "jobs"}),
    ]

def build_retry_script(job_ids: Sequence[str]) -> list[Step]:
    """Create one independent retry call per pending job discovered in phase one."""
    return [Step("retry_job", {"job_id": job_id}) for job_id in job_ids]
