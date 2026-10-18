"""A bounded incident agent making actual MCP stdio calls; no model API required.

The graph ranks candidate playbooks; audit outcomes determine which tool runs
next. These are separate callable phases, not independently reasoning LLM agents.
"""

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_mcp_agent(*, environment=None, pipeline_id="pipeline_eu_payments",
                        stop_after_approval=False, resume=None):
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name("aegis_mcp_server.py"))],
        env=dict(environment or os.environ))
    calls, attempts = [], []
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=40)) as session:
            await session.initialize()

            async def call(name, arguments):
                response = await session.call_tool(name, arguments)
                if response.isError:
                    detail = " ".join(part.text for part in response.content if hasattr(part, "text"))
                    raise RuntimeError(f"MCP tool {name} failed: {detail}")
                value = response.structuredContent
                if value is None:
                    text = next(part.text for part in response.content if hasattr(part, "text"))
                    value = json.loads(text)
                if isinstance(value, dict) and set(value) == {"result"}:
                    value = value["result"]
                calls.append({"tool": name, "status": value.get("status") if isinstance(value, dict) else "RETURNED"})
                return value

            if resume:
                action = await call("get_local_recovery", {"action_id": resume})
                if action["status"] in {"APPROVED", "EXECUTING", "FINALIZING", "STOPPING"}:
                    action = await call("execute_local_recovery", {"action_id": resume})
                return {"status": action["status"], "action_id": resume,
                        "attempts": [action], "tool_calls": calls}
            observed = await call("observe_local_runtime", {"pipeline_id": pipeline_id})
            if not observed["observation"]["errors"] and observed["observation"]["p95_ms"] <= observed["sla_limit_ms"]:
                return {"status": "ALREADY_HEALTHY", "observation": observed["observation"],
                        "attempts": [], "tool_calls": calls}
            candidates = await call("recall_procedural_playbooks", {"pipeline_id": pipeline_id})
            for candidate in candidates:
                action = await call("propose_local_recovery", {"pipeline_id": pipeline_id,
                                                               "playbook_id": candidate["playbook_id"]})
                action = await call("audit_local_recovery", {"action_id": action["id"]})
                if action["status"] == "APPROVED":
                    if not stop_after_approval:
                        action = await call("execute_local_recovery", {"action_id": action["id"]})
                    attempts.append(action)
                    return {"status": action["status"], "action_id": action["id"],
                            "attempts": attempts, "tool_calls": calls}
                attempts.append(action)
            return {"status": "BLOCKED", "attempts": attempts, "tool_calls": calls}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", default="pipeline_eu_payments")
    parser.add_argument("--stop-after-approval", action="store_true")
    parser.add_argument("--resume", help="Execute a previously approved action from another worker")
    args = parser.parse_args()
    result = asyncio.run(run_mcp_agent(pipeline_id=args.pipeline,
        stop_after_approval=args.stop_after_approval, resume=args.resume))
    print(json.dumps(result, indent=2))
