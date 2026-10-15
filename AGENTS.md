# AGENTS.md — Codex & AI Coding Agent Instructions for Aegis-0

## Project Overview
**Aegis-0** is a Counterfactual "Shadow-Graph" Governance & Self-Healing Engine built on **FalkorDB** for the **WeMakeDevs x FalkorDB Graph Hacks** hackathon. It unifies all 3 official tracks in a single graph schema:
- **Track 01 (Connected Data):** Multi-hop service dependency blast radius (`[:DEPENDS_ON*1..4]`), SLA penalty calculation, and ephemeral Shadow-Graph counterfactual simulation (`shadow_sim_xxxx`).
- **Track 02 (Agent Memory & Coordination):** Episodic (`(:Episode)`), Procedural (`(:Playbook)`), and Multi-Agent Handoff locks (`(:HandoffLock)`) coordinating `Planner -> RiskAuditor -> Executor`.
- **Track 03 (Company Brain):** Human ownership mapping (`(:Person)-[:OWNS]->(:DataPipeline)`) and temporal policy evolution (`(:Decision)-[:SUPERSEDES]->(:Decision)`).

## Core Files & Architecture
1. `aegis_falkordb_engine.py`: Core `AegisGraphEngine` class. Handles graph seeding (`seed_tri_layer_universe`), procedural recall + active decision queries (`recall_playbooks_and_company_brain`), isolated Shadow-Graph counterfactual verification (`run_shadow_graph_simulation`), and multi-agent handoff commits (`coordinate_handoff_and_commit`).
2. `aegis_mcp_server.py`: Official Model Context Protocol (`FastMCP`) server wrapping `AegisGraphEngine` as tools, resources (`falkordb://aegis/schema`), and prompts.
3. `aegis_dashboard.py`: Streamlit + `streamlit-agraph` Mission Control UI with Chaos/Policy injection buttons and side-by-side Vector RAG vs. FalkorDB comparison tabs.
4. `test_aegis_engine.py`: Automated verification suite to validate seeding, shadow simulation rejection/approval, and temporal policy superseding.

## Build, Run & Verification Commands
- **Start FalkorDB locally:**
  `docker run -d --name aegis-falkor -p 6379:6379 -p 3000:3000 falkordb/falkordb:latest`
- **Install dependencies:**
  `pip install -r requirements.txt`
- **Run CLI Simulation & Verify Output:**
  `python aegis_falkordb_engine.py`
- **Run Automated Pytest Suite:**
  `pytest test_aegis_engine.py -v`
- **Run Streamlit Mission Control UI:**
  `streamlit run aegis_dashboard.py`
- **Run MCP Server Inspector:**
  `mcp dev aegis_mcp_server.py`

## Guardrails for Codex When Extending Aegis-0
1. **Never log to `stdout` in `aegis_mcp_server.py`:** MCP uses `stdio` for JSON-RPC frames. Always log to `sys.stderr`.
2. **Preserve Ephemeral Shadow Graph Cleanup:** Every call to `run_shadow_graph_simulation` MUST call `shadow_graph.delete()` before returning so ephemeral sandboxes do not leak in FalkorDB.
3. **Preserve `[:SUPERSEDES]` Lineage Filtering:** Active Company Brain decisions must always be filtered via `WHERE NOT (dec)<-[:SUPERSEDES]-()` or `{active: true}` so historical audit trails remain intact.
