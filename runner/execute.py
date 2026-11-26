import contextlib
import json
import sys
from shared.contracts import RepairResult, RunnerRequest, SolutionNotImplemented
from shared.tools import ToolClient


def main():
    request = RunnerRequest.model_validate(json.load(sys.stdin))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            from solution.agent import repair
            result = RepairResult.model_validate(repair(request.context, ToolClient(request.tool_api_url, request.run_id, request.tool_token)))
        json.dump({'result': result.model_dump()}, sys.stdout, ensure_ascii=False)
    except SolutionNotImplemented:
        json.dump({'error': 'solution_not_implemented'}, sys.stdout)
    except Exception:
        json.dump({'error': 'solution_failed'}, sys.stdout)


if __name__ == '__main__':
    main()
