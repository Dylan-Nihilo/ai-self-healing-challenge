from shared.contracts import RepairContext, RepairResult
from shared.tools import ToolClient
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("MODEL_API_KEY"),
    base_url=os.getenv("MODEL_BASE_URL")
)
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-v4.1-flash")


def repair(context: RepairContext, tools: ToolClient) -> RepairResult:
    try:
        log_data = tools.call("read_log")
        recovery_manual = tools.call("read_recovery_manual")
        snapshot_data = tools.call("read_snapshot")

        prompt = """
You are the auto-healing agent for this workstation.
Analyze failure context, log, recovery manual and snapshot.
Find the root cause, output the fix summary.
"""
        full_input = f"""
{prompt}

Failure context:
{context}

Log:
{log_data}

Recovery manual:
{recovery_manual}

Snapshot:
{snapshot_data}
        """

        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": full_input}],
            temperature=0,
            max_tokens=1200
        )
        summary_text = resp.choices[0].message.content.strip()
        return RepairResult(summary=summary_text, outcome="changed")

    except Exception:
        return RepairResult(summary="Repair workflow cannot proceed.", outcome="blocked")