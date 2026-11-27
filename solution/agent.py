"""Fixed-script repair orchestration for the workstation challenge."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
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


def _repair_fixed(context: RepairContext, tools: ToolClient, *, deadline: float | None = None) -> RepairResult:
    """Run the bounded fixed repair script; model mode currently falls back to fixed."""
    deadline = deadline if deadline is not None else time.monotonic() + TOTAL_SECONDS
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
        budget=MAX_TOOL_CALLS - 1,
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
                    budget=MAX_TOOL_CALLS - 1,
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


class _RoundTools:
    """One deadline and actual-call ledger shared by execution and fallback."""

    def __init__(self, tools: ToolClient, deadline: float):
        self.tools = tools
        self.deadline = deadline
        self.used = 0

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        if self.used >= MAX_TOOL_CALLS:
            raise ToolError("tool_budget", "本轮工具调用已达 32 次")
        if self.deadline - time.monotonic() < CALL_RESERVE_SECONDS:
            raise TimeoutError("本轮剩余时间不足 20 秒，停止新调用")
        self.used += 1
        return self.tools.call(name, **args)


def collect_facts(context: RepairContext, tools: Any, report: ExecutionReport, deadline: float) -> None:
    """Collect candidates; the model, not this collector, selects repair resources."""
    failed = set(_failed_checks(context.checks))
    facts = report.facts
    # search 失败时模型也可能需要核对快照（判断是否要恢复目录），因此一并采集候选。
    if "catalog" in failed or "search" in failed:
        facts["snapshots"] = _call(tools, report, "list_snapshots", {}, deadline)["snapshots"]
        details = []
        for candidate in facts["snapshots"]:
            data = _call(tools, report, "read_snapshot", {"snapshot_id": candidate["id"]}, deadline)
            content = json.loads(data["text"])
            documents = content.get("documents", [])
            details.append({"id": candidate["id"], "version": candidate.get("version"),
                            "schema_version": content.get("schema_version"),
                            "documents": len(documents) if isinstance(documents, list) else 0})
        facts["snapshot_details"] = details
        facts["manuals"] = _call(tools, report, "list_manuals", {}, deadline)["manuals"]
    if "export" in failed:
        facts["storage_directories"] = _call(tools, report, "inspect_storage", {}, deadline)["directories"]
        facts["settings"] = _call(tools, report, "read_resource", {"resource": "settings"}, deadline)
        facts["revision"] = facts["settings"]["revision"]
    if "backlog" in failed:
        jobs = _call(tools, report, "read_resource", {"resource": "jobs"}, deadline)["jobs"]
        facts["jobs"] = [{"id": j["id"], "state": j["state"]} for j in jobs]
        facts["job_ids"] = [j["id"] for j in jobs if j["state"] != "done"]


def _collect_investigation_results(report: ExecutionReport) -> None:
    """把调查阶段真实读到的资料回灌到 facts，供修复阶段作为决策依据。"""
    manual_content: dict[str, str] = {}
    snapshot_content: dict[str, Any] = {}
    for evidence in report.evidence:
        if not evidence.ok:
            continue
        if evidence.tool == "read_manual" and isinstance(evidence.data, dict):
            document_id = evidence.args.get("document_id")
            text = evidence.data.get("text")
            if isinstance(document_id, str) and isinstance(text, str) and text.strip():
                manual_content[document_id] = text[:4000]
        elif evidence.tool == "read_snapshot" and isinstance(evidence.data, dict):
            snapshot_id = evidence.data.get("id") or evidence.args.get("snapshot_id")
            text = evidence.data.get("text")
            if isinstance(snapshot_id, str) and isinstance(text, str):
                try:
                    document = json.loads(text)
                except ValueError:
                    continue
                documents = document.get("documents")
                snapshot_content[snapshot_id] = {
                    "version": evidence.data.get("version"),
                    "schema_version": document.get("schema_version"),
                    "documents": len(documents) if isinstance(documents, list) else 0,
                }
    if manual_content:
        report.facts["manual_content"] = manual_content
    if snapshot_content:
        report.facts["snapshot_content"] = snapshot_content


def _investigate(context: RepairContext, tools: Any, report: ExecutionReport,
                 deadline: float, notes: list[str]) -> tuple[str, ExecutionReport]:
    """阶段一：模型只规划调查动作，执行后把真实资料回灌到 facts。

    这是修复阶段能否「基于资料决策」的前提 —— 没有这一阶段，读手册的返回拿不到，
    修复计划就只能来自 prompt 里的先验知识。
    """
    from solution import providers
    from solution.planner import PlannerError, build_prompt, parse_plan

    seconds = deadline - time.monotonic()
    if seconds < 45:
        notes.append("时间不足以完成调查阶段，直接进入修复")
        return "", report

    used = getattr(tools, "used", 0)
    prompt = build_prompt(context, report.facts, [asdict(f) for f in report.failures],
                          max(1, MAX_TOOL_CALLS - used - 12), seconds,
                          mode="investigation")
    try:
        raw = providers.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": "只规划调查动作，不要修复。输出 JSON。"},
        ], timeout=25.0)
        parsed = parse_plan(raw, context, report.facts, max_steps=6, mode="investigation")
    except (providers.ProviderError, PlannerError) as exc:
        notes.append("调查阶段跳过：" + _safe_error(exc))
        return "", report

    before_failures = list(report.failures)
    updated = execute(parsed.steps, tools, deadline=deadline,
                      budget=max(1, MAX_TOOL_CALLS - 10),
                      initial_facts=report.facts, initial_evidence=report.evidence,
                      initial_calls=used)
    updated.failures = before_failures + updated.failures
    _collect_investigation_results(updated)
    collected = sorted(updated.facts.get("manual_content", {}))
    notes.append("调查阶段 %d 步，读到资料：%s" % (len(parsed.steps), ",".join(collected) or "无"))
    return parsed.diagnosis, updated


def _safe_error(exc: Exception) -> str:
    """Do not expose a provider response body, URL or credentials in summaries."""
    from solution.planner import PlannerError
    if isinstance(exc, PlannerError):
        return "planner_validation: " + str(exc)[:240]
    if isinstance(exc, ToolError):
        return exc.code
    import re
    match = re.search(r"HTTP (\d{3})", str(exc))
    return "provider_http_" + match.group(1) if match else type(exc).__name__


def _fallback(context: RepairContext, tools: Any, deadline: float, reason: str, diagnosis: str = "") -> RepairResult:
    try:
        result = _repair_fixed(context, tools, deadline=deadline)
    except (ToolError, TimeoutError, ValueError) as exc:
        return RepairResult(summary=("[模型判断] " + (diagnosis or "不可用") + " | 模型不可用（" + reason
                                     + "），固定剧本未能完成：" + _safe_error(exc)
                                     + "；需要核对剩余预算、资源或权限。")[:1200], outcome="blocked")
    prefix = ("[模型判断] " + diagnosis + " | ") if diagnosis else ""
    result.summary = (prefix + "模型不可用（" + reason + "），本轮降级为固定剧本执行。" + result.summary)[:1200]
    return result


def repair(context: RepairContext, tools: ToolClient) -> RepairResult:
    """Model-directed repair with one replan and explicit bounded fallback."""
    from solution import providers
    from solution.planner import PlannerError, build_prompt, parse_plan

    deadline = time.monotonic() + TOTAL_SECONDS
    bounded = _RoundTools(tools, deadline)
    if os.environ.get("REPAIR_MODE", "fixed").strip().lower() != "model":
        return _repair_fixed(context, bounded, deadline=deadline)
    if not os.environ.get("MODEL_API_KEY", "").strip():
        return _fallback(context, bounded, deadline, "未配置模型，使用固定剧本")

    report = ExecutionReport()
    diagnosis = ""
    notes: list[str] = []
    exhausted_replan = False
    planning_errors: list[dict[str, Any]] = []
    attempt = 0
    try:
        collect_facts(context, bounded, report, deadline)
        # 阶段一：调查。模型只规划读取动作，执行后把真实资料（手册原文、快照摘要）
        # 回灌进 facts，修复阶段才能基于资料而不是先验知识做决策。
        investigation_diagnosis, report = _investigate(context, bounded, report, deadline, notes)
        if investigation_diagnosis:
            diagnosis = investigation_diagnosis
        for attempt in range(2):
            seconds = deadline - time.monotonic()
            if seconds < 45:
                notes.append("剩余时间不足以安全规划并执行；下轮重新调查剩余项")
                break
            # Exactly the same facts object is rendered and passed to the parser.
            facts = report.facts
            prompt = build_prompt(context, facts,
                                  [asdict(f) for f in report.failures] + planning_errors,
                                  MAX_TOOL_CALLS - bounded.used - 1, seconds)
            raw = providers.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": "根据真实事实选择最小修复计划，只输出 JSON。"},
            ], timeout=25.0)
            try:
                parsed = parse_plan(raw, context, facts, max_steps=min(15, MAX_TOOL_CALLS - bounded.used - 1))
            except PlannerError as exc:
                # 计划校验失败不是终点：把具体规则冲突回灌给模型，让它自己改正一次。
                message = str(exc)[:240]
                planning_errors.append({"tool": "<plan_check>", "code": "plan_rejected", "message": message})
                notes.append("计划被拒，回灌重规划：" + message[:120])
                if attempt == 1:
                    raise
                continue
            diagnosis = parsed.diagnosis
            previous_failures = list(report.failures)
            # Agent owns final verification, so it can wait for queued retries first.
            steps = [s for s in parsed.steps if s.tool != "run_self_check"]
            report = execute(steps, bounded, deadline=deadline, budget=MAX_TOOL_CALLS - 1,
                             initial_facts=facts, initial_evidence=report.evidence,
                             initial_calls=bounded.used)
            new_failures = report.failures
            report.failures = previous_failures + new_failures
            if not any(f.severity == "fatal" for f in new_failures):
                break
            if attempt == 1:
                exhausted_replan = True
                notes.append("重规划后仍失败；需要人工核对错误码对应的资源或权限")
    except (providers.ProviderError, PlannerError) as exc:
        if attempt == 0 and deadline - time.monotonic() >= CALL_RESERVE_SECONDS and bounded.used < MAX_TOOL_CALLS:
            return _fallback(context, bounded, deadline, _safe_error(exc), diagnosis)
        exhausted_replan = attempt == 1
        notes.append(_safe_error(exc))
    except (ToolError, TimeoutError, ValueError, KeyError) as exc:
        notes.append("调查或执行失败：" + _safe_error(exc))

    checks = context.checks
    verified = False
    if bounded.used < MAX_TOOL_CALLS and deadline - time.monotonic() >= CALL_RESERVE_SECONDS:
        if any(e.ok and e.tool == "retry_job" for e in report.evidence):
            time.sleep(min(RETRY_SETTLE_SECONDS, max(0.0, deadline - time.monotonic() - CALL_RESERVE_SECONDS)))
        try:
            data = _call(bounded, report, "run_self_check", {}, deadline)
            if not isinstance(data.get("checks"), list) or not data["checks"]:
                raise ValueError("invalid final checks")
            checks = data["checks"]
            verified = True
        except (ToolError, TimeoutError, ValueError) as exc:
            notes.append("最终核验失败：" + _safe_error(exc))
    if not verified:
        notes.append("未完成最终核验；显示的检查为本轮开始状态，下轮须重新读取")
    complete = verified and len(checks) == 4 and all(c.get("ok") is True for c in checks)
    progress = any(e.ok and e.tool in {"restore_catalog", "rebuild_index", "update_settings", "reload_worker", "retry_job"} for e in report.evidence)
    report.calls_used = bounded.used
    summary = ("[模型判断] " + (diagnosis or "规划未完成") + " | " + _summary(context.checks, checks, report, notes))[:1200]
    return RepairResult(summary=summary, outcome="changed" if complete or (progress and not exhausted_replan) else "blocked")
