"""Markdown evidence export for a dashboard triage run."""

from datetime import datetime, timezone
import html


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_handoff_provenance(graph, outcome, simulation):
    """Read only the lock associated with this run's committed simulation."""
    result = graph.query("""
        MATCH (lock:HandoffLock {id: $lock_id, shadow_graph_verified: $shadow_id})
        OPTIONAL MATCH (planner:Agent)-[:INITIATED_HANDOFF]->(lock)
        OPTIONAL MATCH (auditor:Agent)-[:VERIFIED_HANDOFF]->(lock)
        OPTIONAL MATCH (lock)-[:AUTHORIZED_EXECUTION]->(executor:Agent)
        OPTIONAL MATCH (executor)-[:RECORDED]->(episode:Episode {id: $episode_id})
        OPTIONAL MATCH (episode)-[:USED_PLAYBOOK]->(playbook:Playbook)
        RETURN lock.id, lock.status, lock.timestamp, lock.shadow_graph_verified,
               lock.projected_latency_ms, planner.id, auditor.id, executor.id,
               episode.id, playbook.id
    """, {
        "lock_id": outcome["handoff_lock_id"],
        "shadow_id": simulation["shadow_graph_id"],
        "episode_id": outcome["recorded_episode_id"],
    })
    keys = ["handoff_lock_id", "status", "timestamp", "shadow_graph_verified",
            "projected_latency_ms", "planner_id", "auditor_id", "executor_id",
            "episode_id", "playbook_id"]
    return [dict(zip(keys, row)) for row in result.result_set]


def _cell(value):
    if value is None:
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    # Keep graph-sourced values inside one table cell and prevent Markdown/HTML
    # in policy summaries from changing the structure of the audit report.
    text = html.escape(str(value))
    text = text.translate({ord(character): f"&#{ord(character)};"
                           for character in "\\`*[]|"})
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _table(fields):
    return "\n".join(["| Evidence | Recorded value |", "| --- | --- |"] + [
        f"| {_cell(label)} | {_cell(value)} |" for label, value in fields
    ])


def build_compliance_report(logs, outcome, provenance, *, pipeline_id,
                            completed_at, provenance_error=None):
    """Format captured evidence; never infer regulatory certification."""
    sections = [
        "# BaFin Audit #88 — Aegis-0 Compliance Evidence Report",
        _table([("Pipeline", pipeline_id), ("Triage completed (UTC)", completed_at),
                ("Report generated (UTC)", utc_now()),
                ("Simulation attempts", len(logs))]),
        "This report records the latest triage run in this dashboard session. "
        "It is operational evidence, not a regulatory compliance certification. "
        "Shadow graphs are ephemeral; simulation results below were captured before export.",
    ]
    for index, entry in enumerate(logs, 1):
        sim, candidate = entry["simulation"], entry["candidate"]
        sections += [f"## Shadow-Graph simulation — attempt {index}", _table([
            ("Shadow graph ID", sim.get("shadow_graph_id")),
            ("Target service", sim.get("target_service")),
            ("Target name", sim.get("target_name")), ("Region", sim.get("region")),
            ("Projected E2E latency (ms)", sim.get("projected_e2e_latency_ms")),
            ("SLA limit (ms)", sim.get("sla_limit_ms")),
            ("SLA passed", sim.get("sla_passed")),
            ("Policy violation", sim.get("policy_violation")),
            ("Slack traffic freeze violation", sim.get("traffic_freeze_violation")),
            ("Simulation approved", sim.get("simulation_approved")),
            ("Capacity passed", sim.get("capacity_passed")),
            ("Required throughput (TPS)", sim.get("required_throughput_tps")),
            ("Chaos scenario", sim.get("chaos_scenario")),
            ("Incident ID", sim.get("incident_id")),
            ("Rejection reasons", "; ".join(sim.get("rejection_reasons", []))),
            ("Penalty at risk (USD/min)", sim.get("penalty_at_risk_usd")),
            ("Governing decision", sim.get("governing_decision")),
            ("Recalled active decision", candidate.get("active_decision_id")),
            ("Superseded decision", candidate.get("superseded_decision_id")),
            ("Policy summary", candidate.get("active_policy_summary")),
            ("Policy source", candidate.get("policy_source")),
            ("Forbids non-EU routing", candidate.get("forbids_non_eu")),
            ("Playbook", candidate.get("playbook_id")),
            ("Playbook name", candidate.get("playbook_name")),
            ("Historical success rate", candidate.get("historical_success_rate")),
            ("Human domain expert", candidate.get("expert_handle")),
        ])]
        for key, title in [("policy_checks", "Applicable policy evidence"),
                           ("rejection_memory", "RiskAuditor rejection memory"),
                           ("capacity_checks", "Multi-hop capacity checks"),
                           ("sla_checks", "Per-contract SLA checks")]:
            if sim.get(key):
                sections.append(f"### {title}")
                for check in sim[key]:
                    sections.append(_table(list(check.items())))
    sections.append("## Multi-Agent HandoffLock provenance")
    if outcome is None:
        sections.append("No execution was committed in this run; no HandoffLock was created.")
    else:
        sections += ["### Commit result", _table(list(outcome.items()))]
        if provenance_error or not provenance:
            sections.append("**Evidence incomplete:** the matching HandoffLock provenance "
                            "could not be retrieved. The commit result above is session evidence.")
        for trace in provenance:
            sections += ["### Persisted graph trace", _table(list(trace.items()))]
            if any(trace.get(key) is None for key in (
                "planner_id", "auditor_id", "executor_id", "episode_id", "playbook_id"
            )):
                sections.append("**Evidence incomplete:** one or more provenance links are missing.")
            sections.append(
                "Trace relationships: Agent → INITIATED_HANDOFF → HandoffLock; "
                "Agent → VERIFIED_HANDOFF → HandoffLock; "
                "HandoffLock → AUTHORIZED_EXECUTION → Agent; "
                "Agent → RECORDED → Episode → USED_PLAYBOOK → Playbook."
            )
    return "\n\n".join(sections) + "\n"
