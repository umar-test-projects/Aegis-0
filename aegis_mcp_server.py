import os
import sys
import time
import json
import logging
from contextlib import redirect_stdout
from typing import Dict, Any, List
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from aegis_falkordb_engine import AegisGraphEngine
from aegis_company_brain import company_brain_schema, query_company_brain
from aegis_recovery import RecoveryCoordinator
from aegis_runtime import RuntimeClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("AegisMCPServer")

mcp = FastMCP(
    name="Aegis-0 FalkorDB Governance & Self-Healing Engine",
    instructions=(
        "Use these FalkorDB graph tools to inspect infrastructure blast radius (Track 1), "
        "recall episodic/procedural playbooks and coordinate multi-agent handoffs (Track 2), "
        "and verify active Company Brain policies & decision lineage (Track 3). "
        "CRITICAL RULE: Never call commit_multi_agent_handoff without first running "
        "simulate_shadow_failover and verifying that simulation_approved is True."
    ),
)

FALKORDB_HOST = os.getenv("FALKORDB_HOST", "localhost")
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT", 6379))
FALKORDB_PASSWORD = os.getenv("FALKORDB_PASSWORD", None)

engine = AegisGraphEngine(
    host=FALKORDB_HOST,
    port=FALKORDB_PORT,
    password=FALKORDB_PASSWORD,
)
if os.getenv("AEGIS_GRAPH_NAME"):
    engine.main_graph_name = os.environ["AEGIS_GRAPH_NAME"]
    engine.graph = engine.db.select_graph(engine.main_graph_name)


def local_recovery():
    return RecoveryCoordinator(engine, RuntimeClient(
        os.getenv("AEGIS_RUNTIME_URL", "http://127.0.0.1:8787"),
        os.getenv("AEGIS_RUNTIME_TOKEN"),
    ))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def observe_local_runtime(pipeline_id: str = "pipeline_eu_payments") -> Dict[str, Any]:
    """Measure real HTTP requests to the configured loopback synthetic router."""
    coordinator = local_recovery()
    return {"runtime": coordinator.runtime.snapshot(), "observation": coordinator.runtime.measure(),
            "sla_limit_ms": coordinator._sla_limit(pipeline_id), "scope": "synthetic local gateway HTTP"}


@mcp.tool()
def propose_local_recovery(pipeline_id: str, playbook_id: str) -> Dict[str, Any]:
    """Planner: persist a proposal, baseline HTTP observations and router revision."""
    return local_recovery().propose(pipeline_id, playbook_id)


@mcp.tool()
def audit_local_recovery(action_id: str) -> Dict[str, Any]:
    """RiskAuditor: independently load a proposal, run a shadow graph, persist approval/rejection."""
    return local_recovery().audit(action_id)


@mcp.tool()
def execute_local_recovery(action_id: str) -> Dict[str, Any]:
    """Executor: claim a fresh approval, change local routing and verify HTTP before learning.

    Reusing the action ID resumes persisted work. Failed checks disable the demo
    router. This tool never changes real production infrastructure.
    """
    return local_recovery().execute(action_id)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
def get_local_recovery(action_id: str) -> Dict[str, Any]:
    """Read a persisted action, phase trace, audit and HTTP evidence after a worker restart."""
    return RecoveryCoordinator(engine, None).get(action_id)


@mcp.prompt()
def verified_local_incident_triage() -> str:
    return (
        "Observe local runtime first. If errors are zero and measured p95 is within the SLA, stop. "
        "Otherwise recall_procedural_playbooks; for each graph-ranked playbook call "
        "propose_local_recovery and audit_local_recovery. Execute only an APPROVED action "
        "using execute_local_recovery. Stop on VERIFIED, FAILED or STALE. If every proposal "
        "is REJECTED, report BLOCKED with policy evidence. A second worker can resume by "
        "reading get_local_recovery and executing its approved action ID. Report predicted "
        "and measured latency separately; the owner is identified, not messaged."
    )


@mcp.resource("falkordb://aegis/schema")
def get_tri_layer_schema() -> str:
    """Exposes the 3-layer FalkorDB graph schema."""
    schema_doc = company_brain_schema(engine.main_graph_name)
    return json.dumps(schema_doc, indent=2)


@mcp.resource("falkordb://aegis/status/{pipeline_id}")
def get_pipeline_live_status(pipeline_id: str) -> str:
    """Returns the current live health, active route, and downstream SLA risk of a pipeline."""
    cypher = """
    MATCH (p:DataPipeline {id: $pipeline_id})-[r:ROUTES_THROUGH {active: true}]->(svc:Service)-[:LOCATED_IN]->(reg:Region)
    OPTIONAL MATCH (p)<-[:DEPENDS_ON*1..3]-(down:Service)-[:BOUND_BY]->(sla:SLA)
    RETURN p.id, p.status, p.latency_ms, svc.id, svc.name, reg.id, down.id, sla.max_allowed_latency_ms, sla.penalty_per_min_usd
    """
    res = engine.graph.query(cypher, {"pipeline_id": pipeline_id})
    if not res.result_set:
        return json.dumps({"error": f"Pipeline '{pipeline_id}' not found. Run seed_aegis_universe first."})

    row = res.result_set[0]
    return json.dumps(
        {
            "pipeline_id": row[0],
            "status": row[1],
            "current_latency_ms": row[2],
            "active_gateway_id": row[3],
            "active_gateway_name": row[4],
            "region": row[5],
            "downstream_dependent_service": row[6],
            "sla_max_latency_ms": row[7],
            "sla_penalty_per_min_usd": row[8],
        },
        indent=2,
    )


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
))
def query_company_brain_cypher(question: str, limit: int = 50) -> Dict[str, Any]:
    """Translate an ownership, policy or lineage question using falkordb://aegis/schema.

    Executes parameterized templates via GRAPH.RO_QUERY, returning Cypher,
    parameters, evidence rows and a JSON subgraph. Supports audit-service owners,
    entity owners, active policies, predecessors/successors, and superseded today/yesterday
    (UTC). No arbitrary Cypher input is accepted. Maximum 100 rows.
    """
    logger.info("Querying Company Brain through read-only Cypher")
    return query_company_brain(engine.graph, question, limit=limit)


@mcp.tool()
def seed_aegis_universe() -> Dict[str, Any]:
    """Resets and seeds the complete 3-layer FalkorDB graph."""
    logger.info("Seeding Tri-Layer Universe in FalkorDB...")
    with redirect_stdout(sys.stderr):
        engine.seed_tri_layer_universe()
    return {
        "status": "SEEDED",
        "graph_name": engine.main_graph_name,
        "initial_incident": "pipeline_eu_payments degraded at 1420ms (SLA max: 250ms)",
    }


@mcp.tool()
def trace_blast_radius_and_owners(pipeline_id: str = "pipeline_eu_payments") -> Dict[str, Any]:
    """[Track 1 & Track 3] Multi-hop graph traversal that computes downstream blast radius, SLA risk, active policy, and owner."""
    logger.info(f"Tracing blast radius for pipeline: {pipeline_id}")
    cypher = """
    MATCH path = (p:DataPipeline {id: $pipeline_id})<-[:DEPENDS_ON*1..4]-(downstream:Service)
    OPTIONAL MATCH (downstream)-[:BOUND_BY]->(sla:SLA)
    OPTIONAL MATCH (dec:Decision)-[:GOVERNS]->(p)
    WHERE NOT (dec)<-[:SUPERSEDES]-()
    OPTIONAL MATCH (expert:Person)-[owns:OWNS]->(p)
    RETURN
        [n IN nodes(path) | coalesce(n.name, n.id)] AS dependency_chain,
        downstream.id AS impacted_service_id,
        sla.client_tier AS sla_tier,
        sla.max_allowed_latency_ms AS max_latency_ms,
        sla.penalty_per_min_usd AS penalty_per_min_usd,
        dec.id AS active_decision_id,
        dec.summary AS active_policy_summary,
        dec.source_url AS policy_citation,
        expert.name AS owner_name,
        expert.slack_handle AS owner_slack,
        owns.expertise_score AS expertise_score
    ORDER BY owns.expertise_score DESC
    """
    res = engine.graph.query(cypher, {"pipeline_id": pipeline_id})
    impacts = []
    for row in res.result_set:
        impacts.append(
            {
                "dependency_chain": row[0],
                "impacted_service_id": row[1],
                "sla_tier": row[2],
                "max_latency_ms": row[3],
                "penalty_per_min_usd": row[4],
                "active_decision_id": row[5],
                "active_policy_summary": row[6],
                "policy_citation": row[7],
                "owner_name": row[8],
                "owner_slack": row[9],
                "expertise_score": row[10],
            }
        )
    return {"pipeline_id": pipeline_id, "downstream_impacts": impacts}


@mcp.tool()
def recall_procedural_playbooks(pipeline_id: str = "pipeline_eu_payments") -> List[Dict[str, Any]]:
    """[Track 2 & Track 3] Queries Agent Procedural Memory for candidate playbooks paired with active Company Brain decisions."""
    logger.info(f"Recalling procedural playbooks for: {pipeline_id}")
    return engine.recall_playbooks_and_company_brain(pipeline_id)


@mcp.tool()
def simulate_shadow_failover(
    pipeline_id: str,
    candidate_service_id: str,
    playbook_id: str = None,
    incident_id: str = None,
) -> Dict[str, Any]:
    """[Track 1 Core Innovation] Spawns an isolated ephemeral Shadow Graph inside FalkorDB and tests counterfactual mutation."""
    logger.info(f"Running Shadow-Graph Simulation: {pipeline_id} -> {candidate_service_id}")
    return engine.run_shadow_graph_simulation(
        pipeline_id=pipeline_id,
        candidate_service_id=candidate_service_id,
        playbook_id=playbook_id, incident_id=incident_id,
    )


@mcp.tool()
def commit_multi_agent_handoff(
    pipeline_id: str,
    playbook_id: str,
    candidate_service_id: str,
    expert_handle: str,
    policy_source: str,
) -> Dict[str, Any]:
    """[Track 2 Multi-Agent Coordination] Re-verifies simulation, writes (:HandoffLock), mutates live topology, and updates memory."""
    logger.info(f"Executing Multi-Agent Handoff for playbook '{playbook_id}' -> '{candidate_service_id}'")
    sim_report = engine.run_shadow_graph_simulation(
        pipeline_id=pipeline_id,
        candidate_service_id=candidate_service_id,
    )

    if not sim_report["simulation_approved"]:
        return {
            "status": "EXECUTION_BLOCKED_BY_AUDITOR",
            "reason": "Shadow-Graph simulation failed compliance or SLA verification.",
            "simulation_report": sim_report,
        }

    outcome = engine.coordinate_handoff_and_commit(
        pipeline_id=pipeline_id,
        playbook_id=playbook_id,
        sim_report=sim_report,
        expert_handle=expert_handle,
        policy_source=policy_source,
    )
    return {
        "status": "COMMITTED_AND_SELF_HEALED",
        "execution_audit": outcome,
        "verified_simulation": sim_report,
    }


@mcp.tool()
def inject_live_company_decision(
    pipeline_id: str,
    decision_id: str,
    summary: str,
    forbids_non_eu: bool,
    author_handle: str = "@marcus.compliance",
    source_url: str = "https://slack.internal/archives/C02compliance/live",
) -> Dict[str, Any]:
    """[Track 3 Live Temporal Memory Update] Injects a new organizational decision with [:SUPERSEDES] edge."""
    logger.info(f"Injecting new Company Brain decision {decision_id} on {pipeline_id}")
    return engine.inject_regional_decision(
        pipeline_id, decision_id, summary, forbids_non_eu, author_handle, source_url,
    )


@mcp.prompt()
def autonomous_incident_triage(pipeline_id: str = "pipeline_eu_payments") -> str:
    """Generates a structured multi-step prompt instructing the LLM agent to triage an incident using all 3 tracks."""
    return f"""You are the Aegis-0 Autonomous Governance & Self-Healing Agent.
An incident has been reported on data pipeline `{pipeline_id}`.

Execute the following traceable GraphRAG workflow using your FalkorDB MCP tools:
1. Call `trace_blast_radius_and_owners(pipeline_id="{pipeline_id}")` to identify downstream SLA financial exposure, active Company Brain rules, and the primary human owner.
2. Call `recall_procedural_playbooks(pipeline_id="{pipeline_id}")` to retrieve candidate playbooks from Agent Procedural Memory ranked by historical success rate.
3. Iterate through the candidate playbooks and run `simulate_shadow_failover` for each candidate's `candidate_service_id`.
4. Explain why any rejected candidate failed inside the ephemeral Shadow Graph (citing the exact `active_decision_id` and `superseded_decision_id`).
5. For the candidate that passes the Shadow-Graph simulation (`simulation_approved: True`), call `commit_multi_agent_handoff` to execute the self-healing reroute and update procedural memory.
6. Output a final audit table showing the exact Cypher lineage, Handoff Lock ID, latency reduction, and human expert notified."""


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)
