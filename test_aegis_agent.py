"""Exercise actual MCP stdio tool calls against local graph and HTTP services."""

import asyncio
import os

from test_aegis_engine import live_engine


def test_live_mcp_agent_uses_independent_recovery_tools(live_engine):
    from aegis_agent import run_mcp_agent
    from aegis_runtime import DemoRuntime

    live_engine.seed_tri_layer_universe()
    with DemoRuntime() as runtime:
        environment = {**os.environ, "AEGIS_GRAPH_NAME": live_engine.main_graph_name,
                       "AEGIS_RUNTIME_URL": runtime.url, "AEGIS_RUNTIME_TOKEN": runtime.token}
        result = asyncio.run(run_mcp_agent(environment=environment))
        assert result["status"] == "VERIFIED"
        calls = [row["tool"] for row in result["tool_calls"]]
        assert calls[:2] == ["observe_local_runtime", "recall_procedural_playbooks"]
        assert calls.count("audit_local_recovery") == 2
        assert calls.count("execute_local_recovery") == 1
        assert result["attempts"][-1]["observation"]["errors"] == 0
        assert runtime.client().snapshot()["execution_count"] == 1


def test_live_mcp_agent_handoff_can_resume_in_a_second_process(live_engine):
    from aegis_agent import run_mcp_agent
    from aegis_runtime import DemoRuntime

    live_engine.seed_tri_layer_universe()
    with DemoRuntime() as runtime:
        environment = {**os.environ, "AEGIS_GRAPH_NAME": live_engine.main_graph_name,
                       "AEGIS_RUNTIME_URL": runtime.url, "AEGIS_RUNTIME_TOKEN": runtime.token}
        first = asyncio.run(run_mcp_agent(environment=environment, stop_after_approval=True))
        assert first["status"] == "APPROVED"
        assert runtime.client().snapshot()["execution_count"] == 0
        second = asyncio.run(run_mcp_agent(environment=environment, resume=first["action_id"]))
        assert second["status"] == "VERIFIED"
        assert runtime.client().snapshot()["execution_count"] == 1
