"""Tool schemas and the bounded read-only evidence collection phase."""

from __future__ import annotations

import json
from typing import Any

from shared.contracts import RepairContext
from shared.tools import ToolClient, ToolError

WORKSPACE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_resource",
            "description": "Read a bounded workstation resource. Use the returned values rather than assuming names.",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "string",
                        "enum": ["catalog", "settings", "loaded_settings", "jobs", "logs"],
                    }
                },
                "required": ["resource"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_snapshots",
            "description": "List catalog snapshots with their versions and digests.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_snapshot",
            "description": "Read a snapshot before restoring it. Check its schema version and documents.",
            "parameters": {
                "type": "object",
                "properties": {"snapshot_id": {"type": "string"}},
                "required": ["snapshot_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_manuals",
            "description": "List the available recovery manuals.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_manual",
            "description": "Read one recovery manual by its actual document_id.",
            "parameters": {
                "type": "object",
                "properties": {"document_id": {"type": "string"}},
                "required": ["document_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_storage",
            "description": "Inspect real archive destinations. Never invent or hard-code an export directory.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_catalog",
            "description": "Restore the catalog from a verified snapshot. Supply the catalog hash read in this repair run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "snapshot_id": {"type": "string"},
                    "expected_sha256": {"type": "string"},
                },
                "required": ["snapshot_id", "expected_sha256"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rebuild_index",
            "description": "Build the real full-text index from the current valid catalog and source documents.",
            "parameters": {
                "type": "object",
                "properties": {"expected_catalog_sha": {"type": "string"}},
                "required": ["expected_catalog_sha"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_settings",
            "description": "Update the disk archive configuration to an available directory. The revision increments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expected_revision": {"type": "integer"},
                    "export_directory": {"type": "string"},
                },
                "required": ["expected_revision", "export_directory"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_worker",
            "description": "Make the running archive worker load the current settings revision.",
            "parameters": {
                "type": "object",
                "properties": {"expected_revision": {"type": "integer"}},
                "required": ["expected_revision"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_job",
            "description": "Retry an existing failed archive job by its actual job_id. Completed jobs are never duplicated.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_self_check",
            "description": "Run the independent functional checks against the real workstation.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


FINISH_TOOL = {
    "type": "function",
    "function": {
        "name": "finish_repair",
        "description": (
            "Finish when every independent check is green, or block when the bounded tools "
            "cannot complete the repair. Do not finish changed unless all checks pass."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Concise, verifiable actions and final verification result.",
                },
                "outcome": {"type": "string", "enum": ["changed", "blocked"]},
            },
            "required": ["summary", "outcome"],
            "additionalProperties": False,
        },
    },
}


ALL_MODEL_TOOLS = [*WORKSPACE_TOOLS, FINISH_TOOL]


def _short(value: Any, max_string: int = 5000, max_items: int = 30) -> Any:
    """Keep evidence useful without letting one tool response dominate context."""
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "\n[truncated]"
    if isinstance(value, list):
        return [_short(item, max_string, max_items) for item in value[:max_items]]
    if isinstance(value, dict):
        return {str(key): _short(item, max_string, max_items) for key, item in value.items()}
    return value


def _payload(result: dict[str, Any]) -> Any:
    return result.get("data", result) if isinstance(result, dict) else result


def collect_evidence(
    tools: ToolClient, context: RepairContext
) -> tuple[dict[str, Any], int]:
    """Collect facts only. This function never mutates workstation state."""
    observations: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    calls = 0

    def capture(label: str, name: str, **arguments: Any) -> Any:
        nonlocal calls
        calls += 1
        try:
            value = _payload(tools.call(name, **arguments))
            observations[label] = _short(value)
            return value
        except ToolError as error:
            errors.append({"operation": label, "code": error.code, "message": str(error)})
            observations[label] = None
            return None

    capture("catalog", "read_resource", resource="catalog")
    capture("settings", "read_resource", resource="settings")
    capture("loaded_settings", "read_resource", resource="loaded_settings")
    capture("jobs", "read_resource", resource="jobs")
    capture("snapshots", "list_snapshots")
    manuals = capture("manuals", "list_manuals")
    capture("storage", "inspect_storage")
    capture("logs", "read_resource", resource="logs")

    manual_rows = (manuals or {}).get("manuals", []) if isinstance(manuals, dict) else []
    manual_ids = [item.get("id", "") for item in manual_rows if isinstance(item, dict)]
    major = str(context.system_version).split(".", 1)[0]
    preferred = next((item for item in manual_ids if item.endswith(f"-v{major}")), None)
    if not preferred and len(manual_ids) == 1:
        preferred = manual_ids[0]
    if preferred:
        capture("manual", "read_manual", document_id=preferred)

    return {
        "repair_context": context.model_dump(),
        "observations": observations,
        "observation_errors": errors,
    }, calls


def encode_tool_result(value: Any, limit: int = 24000) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= limit:
        return text
    return json.dumps(
        {"truncated": True, "preview": text[:limit]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
