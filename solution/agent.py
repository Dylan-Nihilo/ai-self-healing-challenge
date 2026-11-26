"""Fixed-script repair orchestration for the workstation challenge."""
from __future__ import annotations

import json
import os
import time
from typing import Any

from shared.contracts import RepairContext, RepairResult
from shared.tools import ToolClient, ToolError
from solution.executor import (
    ExecutionReport,
    FailureRecord,
    ToolEvidence,
    build_mixed_script,
    build_retry_script,
    execute,
    failure_severity,
)

TOTAL_SECONDS = 90.0
CALL_RESERVE_SECONDS = 20.0
RETRY_SETTLE_SECONDS = 0.3
MAX_TOOL_CALLS = 32


def _data(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("data")
    if not isinstance(value, dict):
        raise ValueError("tool response did not contain an object data field")
    return value


def _call(tools: ToolClient, report: ExecutionReport, name: str, args: dict[str, Any], deadline: float) -> dict[str, Any]:
    if report.calls_used >= MAX_TOOL_CALLS:
        raise TimeoutError("tool budget exhausted")
    if deadline - time.monotonic() < CALL_RESERVE_SECONDS:
        raise TimeoutError("insufficient time for another tool call")
    report.calls_used += 1
    try:
        data = _data(tools.call(name, **args))
    except ToolError as exc:
        failure = FailureRecord(name, exc.code, str(exc), len(report.evidence), failure_severity(exc.code))
        report.failures.append(failure)
        report.evidence.append(ToolEvidence(len(report.evidence), name, args, False, error=failure))
        raise
    report.evidence.append(ToolEvidence(len(report.evidence), name, args, True, data=data))
    return data


def _select_snapshot(tools: ToolClient, report: ExecutionReport, deadline: float) -> tuple[str, dict[str, Any]]:
    listing = _call(tools, report, "list_snapshots", {}, deadline)
    snapshots = listing.get("snapshots", [])
    if not isinstance(snapshots, list):
        raise ValueError("list_snapshots returned an invalid snapshots list")
    candidates = [item for item in snapshots if isinstance(item, dict) and str(item.get("version", "")).startswith("2.") and isinstance(item.get("id"), str)]
    for candidate in candidates:
        content = _call(tools, report, "read_snapshot", {"snapshot_id": candidate["id"]}, deadline)
        try:
            parsed = json.loads(content.get("text", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        documents = parsed.get("documents") if isinstance(parsed, dict) else None
        if isinstance(parsed, dict) and parsed.get("schema_version") == 2 and isinstance(documents, list) and len(documents) >= 3:
            report.facts["snapshot_id"] = candidate["id"]
            return candidate["id"], {"snapshot": candidate, "snapshot_content": content}
    raise ValueError("no 2.x snapshot has schema_version=2 and at least three documents")


def _select_export_directory(tools: ToolClient, report: ExecutionReport, deadline: float) -> tuple[str, dict[str, Any]]:
    storage = _call(tools, report, "inspect_storage", {}, deadline)
    directories = storage.get("directories", [])
    if not isinstance(directories, list):
        raise ValueError("inspect_storage returned an invalid directories list")
    for directory in directories:
        if isinstance(directory, dict) and directory.get("available") is True and isinstance(directory.get("id"), str):
            report.facts["storage_directory"] = directory["id"]
            report.facts["storage_directories"] = directories
            return directory["id"], {"storage": storage}
    raise ValueError("inspect_storage has no available export directory")


def _failed_checks(checks: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("id", "unknown")) for item in checks if not item.get("ok")]


def _summary(before: list[dict[str, Any]], after: list[dict[str, Any]], report: ExecutionReport, notes: list[str]) -> str:
    before_bad = _failed_checks(before)
    after_bad = _failed_checks(after)
    actions = [f"{item.tool}({'ok' if item.ok else 'failed'})" for item in report.evidence]
    errors = [f"{item.tool}:{item.code}" for item in report.failures] + notes
    status = "all checks passed" if not after_bad else "remaining checks=" + ",".join(after_bad)
    detail = f"Initially failed={','.join(before_bad) or 'none'}. Steps={'; '.join(actions) or 'none'}. Current={status}; calls={report.calls_used}."
    if errors:
        detail += " Issues=" + ";".join(errors) + "."
    return detail[:1200]


def _record_tool_error(report: ExecutionReport, name: str, args: dict[str, Any], exc: ToolError) -> None:
    failure = FailureRecord(name, exc.code, str(exc), len(report.evidence), "fatal")
    report.failures.append(failure)
    report.evidence.append(ToolEvidence(len(report.evidence), name, args, False, error=failure))


def repair(context: RepairContext, tools: ToolClient) -> RepairResult:
    """Run the bounded fixed repair script; model mode currently falls back to fixed."""
    deadline = time.monotonic() + TOTAL_SECONDS
    notes: list[str] = []
    before = context.checks
    report = ExecutionReport()

    mode = os.environ.get("REPAIR_MODE", "fixed").strip().lower()
    if mode not in {"fixed", "model"}:
        notes.append("unknown REPAIR_MODE; used fixed")

    try:
        snapshot_id, snapshot_facts = _select_snapshot(tools, report, deadline)
        export_directory, storage_facts = _select_export_directory(tools, report, deadline)
    except (ToolError, ValueError, TimeoutError) as exc:
        notes.append(f"phase0:{getattr(exc, 'code', type(exc).__name__)}")
        checks = before
        if deadline - time.monotonic() >= CALL_RESERVE_SECONDS and report.calls_used < MAX_TOOL_CALLS:
            try:
                data = _call(tools, report, "run_self_check", {}, deadline)
                checks = data.get("checks", before)
            except (ToolError, TimeoutError) as check_error:
                notes.append(f"check:{getattr(check_error, 'code', type(check_error).__name__)}")
        return RepairResult(summary=_summary(before, checks, report, notes), outcome="blocked")

    phase1_steps = [
        step for step in build_mixed_script(snapshot_id, export_directory)
        if step.tool not in {"list_snapshots", "read_snapshot", "inspect_storage"}
        and not (step.tool == "read_resource" and step.args.get("resource") in {"loaded_settings", "jobs"})
    ]
    initial_facts = {**report.facts, **snapshot_facts, **storage_facts}
    report = execute(
        phase1_steps,
        tools,
        deadline=deadline,
        budget=max(0, MAX_TOOL_CALLS - report.calls_used),
        initial_facts=initial_facts,
        initial_evidence=report.evidence,
        initial_calls=report.calls_used,
    )

    if not report.stopped and not report.deadline_reached and deadline - time.monotonic() >= CALL_RESERVE_SECONDS:
        try:
            jobs_data = _call(tools, report, "read_resource", {"resource": "jobs"}, deadline)
            jobs = jobs_data.get("jobs", [])
            if not isinstance(jobs, list):
                raise ValueError("jobs resource did not contain a list")
            job_records = [{"id": job["id"], "state": job.get("state")} for job in jobs if isinstance(job, dict) and isinstance(job.get("id"), str)]
            report.facts["jobs"] = job_records
            pending = [job["id"] for job in job_records if job["state"] != "done"]
            report.facts["job_ids"] = pending
            if pending:
                remaining = MAX_TOOL_CALLS - report.calls_used
                report = execute(
                    build_retry_script(pending),
                    tools,
                    deadline=deadline,
                    budget=remaining,
                    initial_facts=report.facts,
                    initial_evidence=report.evidence,
                    initial_calls=report.calls_used,
                )
        except ToolError as exc:
            _record_tool_error(report, "read_resource", {"resource": "jobs"}, exc)
            report.stopped = True
        except ValueError as exc:
            notes.append(f"jobs:{exc}")
    elif not report.stopped:
        notes.append("deadline reserve reached before backlog processing")

    checks = before
    if report.calls_used < MAX_TOOL_CALLS and deadline - time.monotonic() >= CALL_RESERVE_SECONDS:
        if any(item.tool == "retry_job" and item.ok for item in report.evidence):
            time.sleep(min(RETRY_SETTLE_SECONDS, max(0.0, deadline - time.monotonic() - CALL_RESERVE_SECONDS)))
        try:
            checks = _call(tools, report, "run_self_check", {}, deadline).get("checks", before)
            if not isinstance(checks, list):
                raise ValueError("self-check returned an invalid checks list")
        except ToolError as exc:
            _record_tool_error(report, "run_self_check", {}, exc)
            notes.append(f"check:{exc.code}")
        except (TimeoutError, ValueError) as exc:
            notes.append(f"check:{type(exc).__name__}")
    else:
        notes.append("deadline or tool budget reserve reached before final check")

    complete = bool(checks) and all(item.get("ok") is True for item in checks)
    changed_tools = {"restore_catalog", "rebuild_index", "update_settings", "reload_worker", "retry_job"}
    progress = any(item.ok and item.tool in changed_tools for item in report.evidence)
    outcome = "changed" if complete or progress else "blocked"
    return RepairResult(summary=_summary(before, checks, report, notes), outcome=outcome)
