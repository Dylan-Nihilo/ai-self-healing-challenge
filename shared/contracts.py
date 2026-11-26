from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class RepairContext(Contract):
    system_version: str
    checks: list[dict[str, Any]]
    previous_summary: str | None = None


class RepairResult(Contract):
    summary: str = Field(min_length=1, max_length=4000)
    outcome: Literal['changed', 'blocked']


class RunnerRequest(Contract):
    context: RepairContext
    run_id: str
    tool_api_url: str
    tool_token: str


class ToolCall(Contract):
    name: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class SolutionNotImplemented(Exception):
    pass
