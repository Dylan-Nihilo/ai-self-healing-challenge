from shared.contracts import RepairContext, RepairResult, SolutionNotImplemented
from shared.tools import ToolClient


def repair(context: RepairContext, tools: ToolClient) -> RepairResult:
    """Connect a real model and implement bounded diagnosis, repair and verification."""
    raise SolutionNotImplemented('Implement the workstation repair agent here.')
