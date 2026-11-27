"""Prompt construction and strict validation for model-generated repair plans."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from solution.executor import Step


SYSTEM_PROMPT = r'''你是“万象硅芯 · AI 自愈工作站”的修复规划器。请根据检查结果和本轮真实工具证据，判断根因并生成最小修复剧本。

只输出一个合法 JSON 对象，不要 Markdown、代码围栏或额外文字。JSON 必须且只能包含：
{
  "diagnosis": "一句话根因判断",
  "plan": [{"tool": "工具名", "args": {}, "why": "简短理由"}],
  "expect": "执行后预期检查状态",
  "if_fail": "执行失败后的具体替代调查或修复思路"
}

当前运行上下文：
- system_version: <<SYSTEM_VERSION>>
- checks: <<CHECKS>>
- previous_summary: <<PREVIOUS_SUMMARY>>
- observed_facts: <<FACTS>>
- execution_failures: <<FAILURES>>
- calls_remaining: <<CALLS_REMAINING>>
- seconds_remaining: <<SECONDS_REMAINING>>

检查 id 仅有 catalog、search、export、backlog；每项含 ok 和 detail。

【恢复资料的使用】
系统带有维护手册。手册存在多个版本，且旧版说明可能与你当前处理的软件版本不匹配。
- 当你不确定恢复流程、目录格式或归档配置的正确做法时，先调用 list_manuals() 查看
  可用手册，再 read_manual(document_id) 读取对应版本。
- 使用前必须核对手册声明适用的版本与 system_version 一致。
- 旧版手册中关于「固定目录名称」的描述可能不适用于当前版本，不要照搬。
- 读取手册属于调查动作，消耗工具预算，通常一次足够，不要重复读取。

【必须以本轮查到的资料为准】
observed_facts 中可能包含以下真实资料，它们是调查阶段实际读取的结果，不是推测：
- manual_content：手册原文（按 document_id 索引）。恢复流程、目录格式、配置写法的正确做法以它为准。
  如果手册中的说明与你的经验或通用做法冲突，以手册为准；如果手册声明的适用版本与 system_version
  不一致，或手册内容与当前故障明显矛盾，应在 diagnosis 中指出并给出你据此调整后的方案。
- snapshot_content：各快照的 schema_version 与 documents 数量，用于判断哪份快照完整可用。
- settings / storage_directories / jobs：当前真实配置与任务状态。
你的修复计划必须建立在这些资料之上。如果某项关键资料缺失，应先安排读取，而不是凭经验假设。

【失败信息与可能根因的对应】
- catalog 报「资料目录无法解析」→ catalog.json 内容损坏，需从快照恢复并重建索引。
- search 报「检索索引不可用」→ 索引表结构损坏，需 rebuild_index（依赖可读的 catalog）。
- export 报「归档工作进程未在时限内完成请求」或「导出目的地不可用」
  → settings 的 export_directory 指向不存在的目录，需 update_settings 后 reload_worker。
- backlog 报「N 个原有归档任务仍未完成」
  → 先让归档能力恢复，再对每个未完成任务 retry_job。
- catalog 恢复后仍失败 → 可能选错了快照（旧版本不完整），需重新核对 schema_version。

可用工具与精确参数（参数必须完整匹配，不允许额外字段）：
- read_resource(resource)，resource ∈ catalog/settings/loaded_settings/jobs/logs
- list_manuals()
- read_manual(document_id)
- list_snapshots()
- read_snapshot(snapshot_id)
- inspect_storage()
- restore_catalog(snapshot_id, expected_sha256)
- rebuild_index(expected_catalog_sha)
- update_settings(expected_revision, export_directory)
- reload_worker(expected_revision)
- retry_job(job_id)
- run_self_check()

运行时事实符号：
- "$catalog_sha"：最近一次成功读取的坏 catalog 原始 sha256。
- "$restored_sha"：restore_catalog 返回的恢复后目录 sha256。
- "$revision"：磁盘 settings 当前 revision；update_settings 后应使用其新 revision。
- "$loaded_revision"：worker 内存配置 revision，仅供判断是否需要 reload。
- "$job_ids"：本轮 jobs 中 state != "done" 的全部任务 id。每个任务需独立 retry_job。
- snapshot_id：必须是 observed_facts.snapshots 中的真实 id；从中选择 version 以 2. 开头、内容显示 schema_version=2 且 documents 至少 3 份的那个。不要写 "$snapshot_id"，也不得编造 id。
- export_directory：必须是 observed_facts.storage_directories 中 available=true 的真实 id。不要写 "$storage_directory"。

强制决策与顺序规则：
1. 只能使用上面的 12 个工具。
2. 仅处理当前失败项，不要修复已经通过的功能。
3. 若 catalog 失败，先 read_resource(resource="catalog") 取得当前 sha；按版本和快照内容选择 schema_version=2 且 documents 至少 3 份的快照，然后 restore_catalog，最后 rebuild_index。若 observed_facts.snapshot_details 已提供各快照的 schema_version 与 documents 数量，直接据此选择，无需再次 read_snapshot；只有 snapshot_details 缺失或信息不足时才调用 read_snapshot。已有 snapshots 时不重复 list_snapshots。
4. restore_catalog 的 snapshot_id 必须是本轮真实观察到的 id；expected_sha256 必须写 "$catalog_sha"。禁止编造运行时 id 或哈希。
5. catalog 损坏时 rebuild_index 必须在 restore_catalog 之后，expected_catalog_sha 写 "$restored_sha"。
   若 catalog 检查已通过（catalog 的 ok=true）而仅 search 失败：**绝对不要调用 restore_catalog**，
   直接 read_resource(resource="catalog") 取得当前目录的 sha256，再调用
   rebuild_index(expected_catalog_sha="$catalog_sha")。校验器会拒绝任何针对已通过项的写操作，
   包括 restore_catalog，即使你已经把它排进了计划。
6. 若 export 失败，使用已有 settings 和 storage_directories（缺失时才读取 settings 并 inspect_storage）；export_directory 必须取自 available=true 目录。先 update_settings(expected_revision="$revision")，再 reload_worker(expected_revision="$revision")，使用更新后的 revision。
7. backlog 若失败，应在归档能力恢复后对所有非 done 任务逐个 retry_job；已有 jobs/job_ids 时直接使用，不必再次读取 jobs。不得猜测 job id。
8. snapshot_id 和 export_directory 是模型必须从 observed_facts 中挑选的真实选择值，不是时序符号。snapshot_id 必须满足 2.x、schema_version=2、documents 至少 3 份；export_directory 必须 available=true。不得编造或用占位符替代。
9. 符号 sha/revision 参数必须按上述符号传递，不得写数字或旧值。
10. 最后通常调用 run_self_check；不要安排超过 2 次。
11. plan 最多 15 个步骤。剩余工具预算 <<CALLS_REMAINING>> 次，剩余时间约 <<SECONDS_REMAINING>> 秒；计划必须在预算内。预算紧张时优先修复影响面最大的失败项，省略次要调查。
12. A 场景（catalog/search/export/backlog 均失败）必须在修复前 read_manual 读取适用于 system_version 的手册；已有 manuals 时无需重复 list_manuals。document_id 必须取自 observed_facts.manuals 的真实 id；缺少手册目录时先由调用方采集，不得编造。不要读取或照搬不匹配的旧手册。
13. C 场景（仅 backlog 失败）只需使用已有 jobs/job_ids（缺失时读取 jobs）、retry 每个非 done 任务并最终自检；不要触碰 catalog 或 worker。
14. 输出必须是 JSON object 格式。字段 diagnosis 应指出根因推断，不要只复述错误；if_fail 应写出失败后的下一种具体调查/恢复方式。
15. observed_facts 中已经包含的读取结果不需要重复调用相同工具，直接使用事实值作为参数或判断依据。catalog 的当前 sha 仍须在恢复前读取；遇到 resource_changed/revision_conflict 时必须重新读取冲突资源，不能复用过期值。
'''


INVESTIGATION_PROMPT = r'''你是“万象硅芯 · AI 自愈工作站”的修复规划器，当前处于【调查阶段】。

本阶段的目标不是修复，而是收集足够证据判断根因。修复计划会在下一阶段、基于你这一阶段
真实读到的资料来制定。因此只做必要的读取，不要尝试任何写操作。

只输出一个合法 JSON 对象，不要 Markdown、代码围栏或额外文字。JSON 必须且只能包含：
{
  "diagnosis": "初步判断：哪些功能失败，可能的方向",
  "plan": [{"tool": "工具名", "args": {}, "why": "读这个是为了确认什么"}],
  "expect": "调查完成后你预计能确定什么",
  "if_fail": "某个调查工具失败时的替代方案"
}

当前运行上下文：
- system_version: <<SYSTEM_VERSION>>
- checks: <<CHECKS>>
- previous_summary: <<PREVIOUS_SUMMARY>>
- observed_facts: <<FACTS>>
- calls_remaining: <<CALLS_REMAINING>>
- seconds_remaining: <<SECONDS_REMAINING>>

【本阶段只允许只读工具】
- read_resource(resource)，resource ∈ catalog / settings / loaded_settings / jobs / logs
- list_manuals()
- read_manual(document_id)
- list_snapshots()
- read_snapshot(snapshot_id)
- inspect_storage()

禁止使用 restore_catalog、rebuild_index、update_settings、reload_worker、retry_job、
run_self_check —— 这些属于修复阶段。

【调查优先级】
1. 若 catalog 或 search 失败：先 list_manuals，再 read_manual 读取与 system_version 匹配的手册。
   正确的恢复流程写在手册里，且手册存在多个版本，必须选择匹配当前版本的那一份。
2. 若 catalog 失败：读取 catalog 当前原文与 sha256；核对快照候选，用 read_snapshot 确认
   每份快照的 schema_version 与 documents 数量。
3. 若 export 失败：读取 settings，并 inspect_storage 确认真实可用目录。
4. 若 backlog 失败：读取 jobs 确认未完成任务及其状态。

【约束】
1. 计划最多 6 步，只做必要读取。
2. observed_facts 中已有的数据不要重复调用相同工具，直接使用事实值。
3. document_id 必须来自 observed_facts.manuals 的真实 id，不得编造。
4. snapshot_id 必须来自 observed_facts.snapshots 的真实 id，不得编造。
5. 输出必须是合法 JSON object，适合 JSON response mode。
'''


INVESTIGATION_TOOLS = frozenset({
    "read_resource", "list_manuals", "read_manual",
    "list_snapshots", "read_snapshot", "inspect_storage",
})


TOOL_ARGUMENTS: dict[str, set[str]] = {
    "read_resource": {"resource"},
    "list_manuals": set(),
    "read_manual": {"document_id"},
    "list_snapshots": set(),
    "read_snapshot": {"snapshot_id"},
    "inspect_storage": set(),
    "restore_catalog": {"snapshot_id", "expected_sha256"},
    "rebuild_index": {"expected_catalog_sha"},
    "update_settings": {"expected_revision", "export_directory"},
    "reload_worker": {"expected_revision"},
    "retry_job": {"job_id"},
    "run_self_check": set(),
}
ALLOWED_SYMBOLS = {
    "$catalog_sha", "$restored_sha", "$revision", "$loaded_revision",
    "$job_ids",
}
RESOURCE_NAMES = {"catalog", "settings", "loaded_settings", "jobs", "logs"}
SENSITIVE_PATTERNS = (
    re.compile(r"sk-", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"api_key", re.IGNORECASE),
    re.compile(r"run_id", re.IGNORECASE),
    re.compile(r"x-repair-token", re.IGNORECASE),
)


class PlannerError(ValueError):
    """The model response is malformed, unsafe, or inconsistent with checks."""


@dataclass(frozen=True)
class ParsedPlan:
    diagnosis: str
    steps: list[Step]
    expect: str
    if_fail: str


def build_prompt(
    context: Any,
    facts: dict[str, Any],
    failures: list[Any],
    calls_remaining: int,
    seconds_remaining: float,
    mode: str = "repair",
) -> str:
    """Render runtime context without interpreting JSON braces or $symbols."""
    template = INVESTIGATION_PROMPT if mode == "investigation" else SYSTEM_PROMPT
    return (
        template
        .replace("<<SYSTEM_VERSION>>", str(context.system_version))
        .replace("<<CHECKS>>", json.dumps(context.checks, ensure_ascii=False, default=str))
        .replace("<<PREVIOUS_SUMMARY>>", context.previous_summary or "无")
        .replace("<<FACTS>>", json.dumps(facts, ensure_ascii=False, default=str))
        .replace("<<FAILURES>>", json.dumps(failures, ensure_ascii=False, default=str))
        .replace("<<CALLS_REMAINING>>", str(max(0, int(calls_remaining))))
        .replace("<<SECONDS_REMAINING>>", f"{max(0.0, float(seconds_remaining)):.0f}")
    )


def _scan_sensitive(value: Any, path: str = "response") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _scan_sensitive(str(key), f"{path}.<key>")
            _scan_sensitive(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_sensitive(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if any(pattern.search(value) for pattern in SENSITIVE_PATTERNS):
            raise PlannerError(f"sensitive content detected at {path}")


def _check_symbol(value: Any, path: str) -> None:
    if isinstance(value, str) and value.startswith("$") and value not in ALLOWED_SYMBOLS:
        raise PlannerError(f"unknown runtime symbol at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _check_symbol(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_symbol(child, f"{path}[{index}]")


def _validate_args(tool: str, args: Any, index: int, facts: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise PlannerError(f"plan[{index}].args must be an object")
    expected = TOOL_ARGUMENTS[tool]
    if set(args) != expected:
        raise PlannerError(f"plan[{index}] has incorrect arguments for {tool}")
    _check_symbol(args, f"plan[{index}].args")
    if tool == "read_resource" and args["resource"] not in RESOURCE_NAMES:
        raise PlannerError(f"plan[{index}] uses an unknown resource")
    for key, value in args.items():
        if not isinstance(value, str):
            raise PlannerError(f"plan[{index}].args.{key} must be a string")
        if key == "expected_sha256" and value != "$catalog_sha":
            raise PlannerError(f"plan[{index}].args.expected_sha256 must use $catalog_sha")
        if key == "expected_catalog_sha" and value not in {"$catalog_sha", "$restored_sha"}:
            raise PlannerError(f"plan[{index}].args.{key} must use a runtime sha symbol")
        if key == "expected_revision" and value != "$revision":
            raise PlannerError(f"plan[{index}].args.expected_revision must use a revision symbol")
        if key == "snapshot_id":
            snapshots = facts.get("snapshots", [])
            known = {s.get("id") for s in snapshots if isinstance(s, dict)}
            if isinstance(facts.get("snapshot_id"), str):
                known.add(facts["snapshot_id"])
            snapshot = facts.get("snapshot")
            if isinstance(snapshot, dict):
                known.add(snapshot.get("id"))
            if not isinstance(value, str) or value not in known:
                raise PlannerError("snapshot_id must be observed in this run's snapshots")
        if key == "export_directory":
            directories = facts.get("storage_directories", facts.get("storage", {}).get("directories", []))
            known = {d.get("id") for d in directories if isinstance(d, dict) and d.get("available") is True}
            if value not in known:
                raise PlannerError("export_directory must be an observed available directory")
        if key == "document_id":
            manuals = facts.get("manuals", [])
            if value not in {m.get("id") for m in manuals if isinstance(m, dict)}:
                raise PlannerError("document_id must be observed in this run's manuals")
        if key == "job_id" and value != "$job_ids" and value not in facts.get("job_ids", []):
            raise PlannerError("job_id must be an observed pending job id")
    return args


def _validate_order(steps: list[Step], facts: dict[str, Any]) -> None:
    tools = [step.tool for step in steps]
    positions: dict[str, list[int]] = {}
    for index, name in enumerate(tools):
        positions.setdefault(name, []).append(index)
    for left, right in (("update_settings", "reload_worker"),):
        if right in positions and (left not in positions or min(positions[left]) > min(positions[right])):
            raise PlannerError(f"{left} must precede {right}")
    if "restore_catalog" in positions and ("rebuild_index" not in positions or max(positions["restore_catalog"]) > min(positions["rebuild_index"])):
        raise PlannerError("catalog restoration must precede index rebuilding")
    def require_before(source: str, target: str, label: str) -> None:
        if target not in positions:
            return
        if source not in positions or min(positions[source]) > min(positions[target]):
            raise PlannerError(f"{label} must precede {target}")

    resource_reads = {
        step.args.get("resource"): index
        for index, step in enumerate(steps)
        if step.tool == "read_resource"
    }
    if "restore_catalog" in positions:
        if resource_reads.get("catalog", len(steps)) > min(positions["restore_catalog"]):
            raise PlannerError("read_resource(catalog) must precede restore_catalog")
        if not facts.get("snapshots"):
            require_before("list_snapshots", "restore_catalog", "snapshot listing")
        for step in steps:
            if step.tool == "restore_catalog":
                verified = any(isinstance(d, dict) and d.get("id") == step.args["snapshot_id"]
                               and d.get("schema_version") == 2 and isinstance(d.get("documents"), int)
                               and d["documents"] >= 3 for d in facts.get("snapshot_details", []))
                if not verified:
                    require_before("read_snapshot", "restore_catalog", "snapshot verification")
    if "rebuild_index" in positions and "restore_catalog" not in positions:
        if resource_reads.get("catalog", len(steps)) > min(positions["rebuild_index"]):
            raise PlannerError("read_resource(catalog) must precede rebuild_index")
    for target in ("update_settings",):
        if target in positions:
            if not (isinstance(facts.get("settings"), dict) and isinstance(facts.get("revision"), int)) and resource_reads.get("settings", len(steps)) > min(positions[target]):
                raise PlannerError(f"read_resource(settings) must precede {target}")
            if not facts.get("storage_directories") and ("inspect_storage" not in positions or min(positions["inspect_storage"]) > min(positions[target])):
                raise PlannerError("inspect_storage must precede update_settings")
    if "retry_job" in positions and not isinstance(facts.get("job_ids"), list) and resource_reads.get("jobs", len(steps)) > min(positions["retry_job"]):
        raise PlannerError("read_resource(jobs) must precede retry_job")
    if "read_manual" in positions and not facts.get("manuals"):
        require_before("list_manuals", "read_manual", "list_manuals")
    if "retry_job" in positions:
        retry_at = min(positions["retry_job"])
        worker_steps = [i for name in ("update_settings", "reload_worker") for i in positions.get(name, [])]
        if worker_steps and max(worker_steps) > retry_at:
            raise PlannerError("worker repair must precede retry_job")
    if "run_self_check" in positions and max(positions["run_self_check"]) != len(steps) - 1:
        raise PlannerError("run_self_check may only appear at the end")
    if len(positions.get("run_self_check", [])) > 1:
        raise PlannerError("plan may contain at most one run_self_check")


def _covered_failures(steps: list[Step], failed: set[str], facts: dict[str, Any]) -> set[str]:
    names = {step.tool for step in steps}
    covered: set[str] = set()
    if "catalog" in failed and "restore_catalog" in names and "rebuild_index" in names:
        covered.add("catalog")
    if "search" in failed and "rebuild_index" in names:
        covered.add("search")
    if "export" in failed and {"update_settings", "reload_worker"}.issubset(names):
        covered.add("export")
    jobs_known = isinstance(facts.get("job_ids"), list) or any(s.tool == "read_resource" and s.args.get("resource") == "jobs" for s in steps)
    if "backlog" in failed and jobs_known and "retry_job" in names:
        covered.add("backlog")
    return covered


def parse_plan(
    raw: str,
    context: Any,
    facts: dict[str, Any] | None = None,
    *,
    max_steps: int = 15,
    mode: str = "repair",
) -> ParsedPlan:
    """Parse, security-scan, validate and convert model JSON to executor Steps."""
    if not isinstance(raw, str) or not raw.strip():
        raise PlannerError("model response is empty")
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"invalid JSON response at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise PlannerError("response must be a JSON object")
    _scan_sensitive(payload)

    required = {"diagnosis", "plan", "expect", "if_fail"}
    if set(payload) != required:
        raise PlannerError("response must contain exactly diagnosis, plan, expect, and if_fail")
    for field_name in ("diagnosis", "expect", "if_fail"):
        value = payload[field_name]
        if not isinstance(value, str) or not value.strip() or len(value) > 1200:
            raise PlannerError(f"{field_name} must be a non-empty string of at most 1200 characters")

    raw_steps = payload["plan"]
    if not isinstance(raw_steps, list) or not raw_steps or len(raw_steps) > max_steps:
        raise PlannerError(f"plan must contain 1..{max_steps} steps")
    steps: list[Step] = []
    for index, item in enumerate(raw_steps):
        if not isinstance(item, dict) or set(item) != {"tool", "args", "why"}:
            raise PlannerError(f"plan[{index}] must contain exactly tool, args, and why")
        tool = item["tool"]
        why = item["why"]
        if not isinstance(tool, str) or tool not in TOOL_ARGUMENTS:
            raise PlannerError(f"plan[{index}] names an unavailable tool")
        if not isinstance(why, str) or len(why) > 500:
            raise PlannerError(f"plan[{index}].why must be a string of at most 500 characters")
        args = _validate_args(tool, item["args"], index, facts or {})
        if tool == "retry_job" and args["job_id"] == "$job_ids":
            job_ids = (facts or {}).get("job_ids")
            if not isinstance(job_ids, list):
                raise PlannerError("cannot expand $job_ids without observed pending job ids")
            for job_id in job_ids:
                if not isinstance(job_id, str) or not job_id:
                    raise PlannerError("observed job_ids contains an invalid id")
                steps.append(Step("retry_job", {"job_id": job_id}, why=f"retry pending job {job_id[:8]} (expanded from $job_ids)"))
        else:
            steps.append(Step(tool, dict(args), why))
    if not steps:
        raise PlannerError("plan expanded to no executable steps")
    if len(steps) > max_steps:
        raise PlannerError(f"expanded plan exceeds {max_steps} steps")

    if mode == "investigation":
        names = {step.tool for step in steps}
        illegal = names - INVESTIGATION_TOOLS
        if illegal:
            raise PlannerError(
                "investigation stage may only use read-only tools; rejected: " + ", ".join(sorted(illegal))
            )
        return ParsedPlan(
            diagnosis=payload["diagnosis"].strip(),
            steps=steps,
            expect=payload["expect"].strip(),
            if_fail=payload["if_fail"].strip(),
        )

    _validate_order(steps, facts or {})

    checks = getattr(context, "checks", [])
    passed = {str(check.get("id")) for check in checks if isinstance(check, dict) and check.get("ok")}
    names = {step.tool for step in steps}
    if "catalog" in passed and "restore_catalog" in names:
        raise PlannerError("catalog already passed but plan restores it")
    if "export" in passed and "update_settings" in names:
        raise PlannerError("export already passed but plan rewrites settings")
    failed = {str(check.get("id")) for check in checks if isinstance(check, dict) and not check.get("ok")}
    unknown_failed = failed - {"catalog", "search", "export", "backlog"}
    if unknown_failed:
        raise PlannerError(f"unknown failed checks: {sorted(unknown_failed)}")
    missing = failed - _covered_failures(steps, failed, facts or {})
    if missing:
        raise PlannerError(f"plan does not address failed checks: {sorted(missing)}")
    if {"catalog", "search", "export", "backlog"}.issubset(failed):
        # 两阶段架构下，手册可能在调查阶段已读过（facts.manual_content 存在），
        # 此时修复阶段无需重复读取，直接视为已满足资料要求。
        manual_already_read = bool((facts or {}).get("manual_content"))
        if "read_manual" not in names and not manual_already_read:
            raise PlannerError("all-failed plan must read an applicable recovery manual using observed manuals")
        if "read_manual" in names:
            read_at = next(i for i, step in enumerate(steps) if step.tool == "read_manual")
            list_at = next((i for i, step in enumerate(steps) if step.tool == "list_manuals"), -1)
            writes = {"restore_catalog", "rebuild_index", "update_settings", "reload_worker", "retry_job"}
            if list_at > read_at or any(step.tool in writes for step in steps[:read_at]):
                raise PlannerError("manual investigation must precede repair operations")
    if failed == {"backlog"}:
        allowed = {"read_resource", "retry_job", "run_self_check"}
        if names - allowed or not any(step.tool == "run_self_check" for step in steps):
            raise PlannerError("backlog-only plan must only read jobs, retry pending jobs, and self-check")

    return ParsedPlan(
        diagnosis=payload["diagnosis"].strip(),
        steps=steps,
        expect=payload["expect"].strip(),
        if_fail=payload["if_fail"].strip(),
    )
