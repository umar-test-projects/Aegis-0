import os
import json
import hashlib
import math
import time
import uuid
from collections import deque
from typing import Dict, Any, List
from falkordb import FalkorDB


class AegisGraphEngine:
    """
    Aegis-0: Counterfactual Shadow-Graph Governance & Self-Healing Engine.
    Unifies Track 1 (Connected Data), Track 2 (Agent Memory & Coordination),
    and Track 3 (Company Brain) inside FalkorDB.
    """

    CHAOS_SCENARIOS = {
        "ledger_lock": "Multi-region cascading ledger database lock",
        "throughput_spike": "Payment throughput spike / Zurich DR overload",
        "sla_upgrade": "Conflicting Platinum SLA tier upgrade",
    }

    def __init__(self, host: str = "localhost", port: int = 6379, password: str = None):
        self.db = FalkorDB(host=host, port=port, password=password)
        self.main_graph_name = "aegis_production_brain"
        self.graph = self.db.select_graph(self.main_graph_name)

    # Capture operational state in one read, and reuse the same expression in
    # the commit query so checking freshness and writing are atomic in FalkorDB.
    # The conservative scope includes all operational nodes, not agent memory.
    _INPUT_STATE = """
        MATCH (n) WHERE n:Service OR n:DataPipeline OR n:Region OR n:SLA OR n:Decision
        WITH n, CASE WHEN size(keys(properties(n))) = 0 THEN [null]
                     ELSE keys(properties(n)) END AS property_keys
        UNWIND property_keys AS property_key
        WITH n, property_key ORDER BY id(n), property_key
        WITH n, collect(CASE WHEN property_key IS NULL THEN null
                            ELSE [property_key, properties(n)[property_key]] END) AS property_pairs
        WITH n, property_pairs ORDER BY id(n)
        WITH collect([id(n), labels(n), properties(n)]) AS input_nodes,
             collect([id(n), labels(n), property_pairs]) AS canonical_input_nodes
        OPTIONAL MATCH (a)-[r]->(b)
        WHERE type(r) IN ['DEPENDS_ON', 'BOUND_BY', 'ROUTES_THROUGH',
                         'CANDIDATE_FAILOVER', 'LOCATED_IN', 'GOVERNS', 'SUPERSEDES']
        WITH input_nodes, canonical_input_nodes, a, r, b,
             CASE WHEN r IS NULL OR size(keys(properties(r))) = 0 THEN [null]
                  ELSE keys(properties(r)) END AS edge_keys
        UNWIND edge_keys AS edge_key
        WITH input_nodes, canonical_input_nodes, a, r, b, edge_key ORDER BY id(r), edge_key
        WITH input_nodes, canonical_input_nodes, a, r, b,
             collect(CASE WHEN edge_key IS NULL THEN null
                          ELSE [edge_key, properties(r)[edge_key]] END) AS edge_pairs
        WITH input_nodes, canonical_input_nodes, a, r, b, edge_pairs ORDER BY id(r)
        WITH input_nodes, canonical_input_nodes,
             collect(CASE WHEN r IS NULL THEN null ELSE
                [id(r), id(a), id(b), type(r), properties(r)] END) AS input_edges,
             collect(CASE WHEN r IS NULL THEN null ELSE
                [id(r), id(a), id(b), type(r), edge_pairs] END) AS canonical_input_edges
    """

    # Preserve the database's own float representation across persisted JSON
    # reports. Re-parsing floating-point query parameters can change their last
    # bits, making unchanged measured properties compare unequal in Cypher.
    _INPUT_FRESH = """CASE WHEN $canonical_nodes IS NULL THEN
        input_nodes = $input_nodes AND input_edges = $input_edges
        ELSE toJSON(canonical_input_nodes) = $canonical_nodes
             AND toJSON(canonical_input_edges) = $canonical_edges END"""

    def _simulation_inputs(self):
        rows = self.graph.ro_query(self._INPUT_STATE +
                                   " RETURN input_nodes, input_edges, toJSON(canonical_input_nodes), toJSON(canonical_input_edges)").result_set
        if not rows:
            raise ValueError("Operational graph is empty")
        return {"nodes": rows[0][0], "edges": rows[0][1],
                "canonical_nodes": rows[0][2], "canonical_edges": rows[0][3]}

    def inject_regional_decision(self, pipeline_id, decision_id, summary,
                                 forbids_non_eu, author_handle="@marcus.compliance",
                                 source_url="slack://regional-policy"):
        """Replace regional policy once while retaining any traffic freeze."""
        rows = self.graph.query("""
            MATCH (p:DataPipeline {id: $pipeline})
            MATCH (author:Person {slack_handle: $author})
            OPTIONAL MATCH (existing:Decision {id: $id})
            WITH p, author, existing WHERE existing IS NULL
            OPTIONAL MATCH (old:Decision)-[:GOVERNS]->(p)
            WHERE NOT (old)<-[:SUPERSEDES]-()
            WITH p, author, collect(DISTINCT old) AS prior,
                 max(CASE WHEN old.traffic_blocked = true THEN 1 ELSE 0 END) AS blocked
            CREATE (decision:Decision {id: $id, summary: $summary, active: true,
                timestamp: $ts, source_type: 'Regional_Policy_Update', source_url: $source,
                forbids_non_eu: $regional, traffic_blocked: blocked = 1})
            CREATE (author)-[:AUTHORED]->(decision)-[:GOVERNS]->(p)
            FOREACH (old IN prior |
                SET old.active = false
                CREATE (decision)-[:SUPERSEDES {reason: 'Regional policy update'}]->(old))
            RETURN decision.id, [old IN prior | old.id], decision.traffic_blocked
        """, {"pipeline": pipeline_id, "id": decision_id, "summary": summary,
                "author": author_handle, "source": source_url,
                "regional": forbids_non_eu, "ts": int(time.time())}).result_set
        if not rows:
            raise ValueError("Decision ID already exists, or pipeline/author is missing")
        return {"status": "DECISION_RECORDED", "new_decision_id": rows[0][0],
                "superseded_decision_ids": rows[0][1],
                "superseded_decision_id": rows[0][1][0] if rows[0][1] else None,
                "forbids_non_eu": forbids_non_eu, "traffic_blocked": rows[0][2]}

    def compute_service_criticality(self) -> List[Dict[str, Any]]:
        """Rank services by structural blast-radius risk without modifying the graph.

        Uses the unweighted directed Service-[:DEPENDS_ON]->Service graph;
        pipelines, routing edges, parallel edges and self-dependencies are excluded.
        All services, including isolated ones, contribute to normalization.
        Degree centrality is incoming degree / (N - 1): the fraction of services
        directly depending on this service. Directed betweenness is the fraction
        of shortest paths through it, normalized by (N - 1) * (N - 2).
        Criticality is the equal-weight mean of these two centralities, not an
        outage probability or a count of all transitively affected services.
        Results sort by descending score, then service ID for reproducible ties.
        Exact Brandes betweenness takes O(V * (V + E)) time and O(V + E) space.
        """
        query = """
        MATCH (service:Service)
        OPTIONAL MATCH (service)-[:DEPENDS_ON]->(dependency:Service)
        RETURN service.id, service.name, dependency.id
        """
        rows = self.graph.query(query).result_set
        names = {}
        adjacency = {}
        for service_id, service_name, dependency_id in rows:
            names[service_id] = service_name
            adjacency.setdefault(service_id, set())
            if dependency_id is not None and dependency_id != service_id:
                adjacency[service_id].add(dependency_id)
                adjacency.setdefault(dependency_id, set())

        count = len(adjacency)
        incoming = dict.fromkeys(adjacency, 0)
        betweenness = dict.fromkeys(adjacency, 0.0)
        for dependencies in adjacency.values():
            for dependency in dependencies:
                incoming[dependency] += 1

        # Brandes' algorithm: accumulate each source's shortest-path fractions.
        for source in adjacency:
            stack = []
            predecessors = {node: [] for node in adjacency}
            paths = dict.fromkeys(adjacency, 0)
            paths[source] = 1
            distance = {source: 0}
            queue = deque([source])
            while queue:
                node = queue.popleft()
                stack.append(node)
                for neighbor in adjacency[node]:
                    if neighbor not in distance:
                        distance[neighbor] = distance[node] + 1
                        queue.append(neighbor)
                    if distance[neighbor] == distance[node] + 1:
                        paths[neighbor] += paths[node]
                        predecessors[neighbor].append(node)

            contribution = dict.fromkeys(adjacency, 0.0)
            while stack:
                node = stack.pop()
                for predecessor in predecessors[node]:
                    contribution[predecessor] += (
                        paths[predecessor] / paths[node]
                    ) * (1.0 + contribution[node])
                if node != source:
                    betweenness[node] += contribution[node]

        ranked = []
        for service_id in adjacency:
            degree = incoming[service_id] / (count - 1) if count > 1 else 0.0
            between = (
                betweenness[service_id] / ((count - 1) * (count - 2))
                if count > 2 else 0.0
            )
            ranked.append({
                "service_id": service_id,
                "service_name": names.get(service_id),
                "in_degree": incoming[service_id],
                "out_degree": len(adjacency[service_id]),
                "degree_centrality": degree,
                "betweenness_centrality": between,
                "criticality_score": (degree + between) / 2.0,
            })
        return sorted(ranked, key=lambda row: (-row["criticality_score"], row["service_id"]))

    def seed_tri_layer_universe(self) -> None:
        """
        Clears and seeds the 3-layer graph architecture:
        - Layer A: Company Brain (People, Decisions with [:SUPERSEDES], Policies)
        - Layer B: Connected Service Topology (Pipelines, Microservices, Regions, SLAs)
        - Layer C: Agent Memory & Coordination (Agents, Episodes, Playbooks, HandoffLocks)
        """
        print(f"[*] Resetting and seeding '{self.main_graph_name}'...")
        try:
            self.graph.delete()
        except Exception:
            pass  # Graph does not exist yet
        self.graph = self.db.select_graph(self.main_graph_name)

        seed_cypher = """
        // =================================================================
        // LAYER B: CONNECTED DATA TOPOLOGY (Track 1)
        // =================================================================
        CREATE (reg_eu:Region {id: 'eu-central-1', name: 'Frankfurt EU', gdpr_compliant: true})
        CREATE (reg_us:Region {id: 'us-east-1', name: 'N. Virginia US', gdpr_compliant: false})

        CREATE (pipe_eu:DataPipeline {
            id: 'pipeline_eu_payments',
            name: 'EU Real-Time Payment Stream',
            status: 'DEGRADED',
            latency_ms: 1420,
            throughput_tps: 4500
        })

        CREATE (gw_primary:Service {
            id: 'svc_eu_gateway_primary',
            name: 'EU Payment Gateway Primary',
            tier: 1,
            region: 'eu-central-1',
            base_latency_ms: 95,
            current_load_pct: 94,
            max_throughput_tps: 12000,
            status: 'CONGESTED'
        })

        CREATE (gw_us_fallback:Service {
            id: 'svc_us_failover_fast',
            name: 'US East Fast Failover Node',
            tier: 1,
            region: 'us-east-1',
            base_latency_ms: 45,
            current_load_pct: 22,
            max_throughput_tps: 20000,
            status: 'HEALTHY'
        })

        CREATE (gw_eu_backup:Service {
            id: 'svc_eu_zurich_dr',
            name: 'EU Sovereign DR Node',
            tier: 1,
            region: 'eu-central-1',
            base_latency_ms: 70,
            current_load_pct: 31,
            max_throughput_tps: 12000,
            status: 'HEALTHY'
        })

        CREATE (svc_ledger:Service {
            id: 'svc_core_ledger',
            name: 'Global Settlement Ledger',
            tier: 0,
            region: 'eu-central-1',
            base_latency_ms: 30,
            current_load_pct: 50,
            max_throughput_tps: 20000,
            status: 'HEALTHY'
        })

        CREATE (svc_checkout:Service {
            id: 'svc_merchant_checkout',
            name: 'Merchant Checkout API',
            tier: 1,
            region: 'eu-central-1',
            base_latency_ms: 40,
            current_load_pct: 65,
            max_throughput_tps: 20000,
            status: 'AT_RISK'
        })

        CREATE (sla_tier0:SLA {
            id: 'sla_enterprise_eu',
            client_tier: 'Enterprise FinTech',
            max_allowed_latency_ms: 250,
            penalty_per_min_usd: 15000
        })

        // Connect Topology
        CREATE (pipe_eu)-[:ROUTES_THROUGH {active: true, weight: 1.0, load_contribution_pct: 37.5}]->(gw_primary)
        CREATE (pipe_eu)-[:CANDIDATE_FAILOVER {route_id: 'route_us_01'}]->(gw_us_fallback)
        CREATE (pipe_eu)-[:CANDIDATE_FAILOVER {route_id: 'route_eu_02'}]->(gw_eu_backup)

        CREATE (gw_primary)-[:LOCATED_IN]->(reg_eu)
        CREATE (gw_us_fallback)-[:LOCATED_IN]->(reg_us)
        CREATE (gw_eu_backup)-[:LOCATED_IN]->(reg_eu)

        CREATE (svc_ledger)-[:DEPENDS_ON {criticality: 0.95}]->(pipe_eu)
        CREATE (svc_checkout)-[:DEPENDS_ON {criticality: 0.90}]->(svc_ledger)
        CREATE (svc_checkout)-[:BOUND_BY]->(sla_tier0)

        // =================================================================
        // LAYER A: COMPANY BRAIN & TEMPORAL DECISIONS (Track 3)
        // =================================================================
        CREATE (eng_sarah:Person {
            id: 'usr_sarah',
            name: 'Sarah Chen',
            role: 'Principal Staff SRE',
            slack_handle: '@sarah.chen'
        })
        CREATE (eng_marcus:Person {
            id: 'usr_marcus',
            name: 'Marcus Vance',
            role: 'VP Data Compliance',
            slack_handle: '@marcus.compliance'
        })

        // Historical Decision (Superseded)
        CREATE (dec_old:Decision {
            id: 'DEC-2026-101',
            summary: 'Allow cross-Atlantic failover to us-east-1 during EU traffic spikes.',
            source_type: 'Jira_RFC',
            source_url: 'https://jira.internal/browse/ARCH-101',
            timestamp: 1759276800,
            active: false,
            forbids_non_eu: false
        })

        // Latest Live Decision from Slack (Supersedes DEC-2026-101)
        CREATE (dec_new:Decision {
            id: 'DEC-2026-409',
            summary: 'STRICT FREEZE: EU payment streams must remain strictly within GDPR-compliant EU regions due to BaFin Audit #88.',
            source_type: 'Slack_Thread',
            source_url: 'https://slack.internal/archives/C02compliance/p1760480000',
            timestamp: 1760480000,
            active: true,
            forbids_non_eu: true
        })

        // Connect Company Brain to Topology
        CREATE (eng_sarah)-[:AUTHORED]->(dec_old)
        CREATE (eng_marcus)-[:AUTHORED]->(dec_new)
        CREATE (dec_new)-[:SUPERSEDES {reason: 'Regulatory Audit #88'}]->(dec_old)
        CREATE (dec_old)-[:GOVERNS]->(pipe_eu)
        CREATE (dec_new)-[:GOVERNS]->(pipe_eu)

        CREATE (eng_sarah)-[:OWNS {expertise_score: 0.98, commits_30d: 47}]->(pipe_eu)
        CREATE (eng_sarah)-[:OWNS {expertise_score: 0.91, commits_30d: 29}]->(svc_checkout)
        CREATE (eng_marcus)-[:OWNS {expertise_score: 0.85, commits_30d: 12}]->(sla_tier0)

        // =================================================================
        // LAYER C: AGENT MEMORY & MULTI-AGENT COORDINATION (Track 2)
        // =================================================================
        CREATE (ag_planner:Agent {id: 'agent_planner', role: 'Planner', model: 'Autonomous-Strategist'})
        CREATE (ag_auditor:Agent {id: 'agent_auditor', role: 'RiskAuditor', model: 'Compliance-Verifier'})
        CREATE (ag_executor:Agent {id: 'agent_executor', role: 'Executor', model: 'Infra-Mutator'})

        CREATE (pb_us_failover:Playbook {
            id: 'pb_reroute_us_east',
            name: 'Cross-Region US Fast Failover',
            target_route: 'svc_us_failover_fast',
            success_rate: 0.94,
            runs: 18,
            baseline_success_rate: 0.94,
            baseline_runs: 18
        })

        CREATE (pb_eu_failover:Playbook {
            id: 'pb_reroute_eu_sovereign',
            name: 'Intra-EU Sovereign DR Failover',
            target_route: 'svc_eu_zurich_dr',
            success_rate: 0.89,
            runs: 9,
            baseline_success_rate: 0.89,
            baseline_runs: 9
        })

        CREATE (ep_past:Episode {
            id: 'ep_session_882',
            trigger_event: 'pipeline_eu_payments latency > 1000ms',
            chosen_playbook: 'pb_reroute_us_east',
            outcome: 'SUCCESS',
            timestamp: 1759900000
        })

        CREATE (ag_planner)-[:RECORDED]->(ep_past)-[:USED_PLAYBOOK]->(pb_us_failover)
        CREATE (pb_us_failover)-[:APPLIES_TO]->(pipe_eu)
        CREATE (pb_eu_failover)-[:APPLIES_TO]->(pipe_eu)
        """
        self.graph.query(seed_cypher)
        print("[+] Seed complete! Tri-Layer Universe initialized inside FalkorDB.\n")

    def recall_playbooks_and_company_brain(self, pipeline_id: str) -> List[Dict[str, Any]]:
        """
        Track 2 + Track 3 Query:
        Retrieves candidate procedural playbooks sorted by historical success rate,
        while traversing the Company Brain for active (non-superseded) decisions
        and identifying the top human domain expert.
        """
        query = """
        MATCH (p:DataPipeline {id: $pipeline_id})<-[:APPLIES_TO]-(pb:Playbook)
        OPTIONAL MATCH (dec:Decision)-[:GOVERNS]->(p)
        WHERE NOT (dec)<-[:SUPERSEDES]-()
        OPTIONAL MATCH (old_dec:Decision)<-[:SUPERSEDES]-(dec)
        OPTIONAL MATCH (owner:Person)-[owns:OWNS]->(p)
        RETURN
            pb.id AS playbook_id,
            pb.name AS playbook_name,
            pb.target_route AS candidate_service_id,
            pb.success_rate AS historical_success_rate,
            dec.id AS active_decision_id,
            dec.summary AS active_policy_summary,
            dec.source_url AS policy_source,
            dec.forbids_non_eu AS forbids_non_eu,
            old_dec.id AS superseded_decision_id,
            owner.slack_handle AS expert_handle,
            owns.expertise_score AS expertise_score
        ORDER BY pb.success_rate DESC, owns.expertise_score DESC
        """
        res = self.graph.query(query, {"pipeline_id": pipeline_id})
        candidates = []
        for row in res.result_set:
            candidates.append({
                "playbook_id": row[0],
                "playbook_name": row[1],
                "candidate_service_id": row[2],
                "historical_success_rate": row[3],
                "active_decision_id": row[4],
                "active_policy_summary": row[5],
                "policy_source": row[6],
                "forbids_non_eu": row[7],
                "superseded_decision_id": row[8],
                "expert_handle": row[9],
                "expertise_score": row[10],
            })
        weights = {}
        for candidate in candidates:
            playbook_id = candidate["playbook_id"]
            if playbook_id not in weights:
                decision_ids = self._active_memory_policies(pipeline_id, candidate["candidate_service_id"])
                context = self._policy_context(pipeline_id, decision_ids)
                weight_rows = self.graph.ro_query("""
                    MATCH (pb:Playbook {id: $playbook_id})
                    OPTIONAL MATCH (pb)-[:HAS_CONTEXT]->(context:PlaybookContext {policy_context: $context})
                    RETURN coalesce(context.success_rate, pb.baseline_success_rate, pb.success_rate)
                """, {"playbook_id": playbook_id, "context": context}).result_set
                weights[playbook_id] = (weight_rows[0][0], context)
            candidate["historical_success_rate"], candidate["policy_context"] = weights[playbook_id]
        ranked = sorted(candidates, key=lambda candidate: (
            -candidate["historical_success_rate"], -(candidate["expertise_score"] or 0), candidate["playbook_id"],
        ))
        # Ownership and policy joins can produce several rows for one playbook.
        # Attempt each playbook once, retaining its highest-ranked expert.
        unique = {}
        for candidate in ranked:
            unique.setdefault(candidate["playbook_id"], candidate)
        return list(unique.values())

    @staticmethod
    def _policy_context(pipeline_id, decision_ids):
        payload = json.dumps([pipeline_id, sorted(set(decision_ids))], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _active_memory_policies(self, pipeline_id, candidate_id):
        rows = self.graph.ro_query("""
            MATCH (p:DataPipeline {id: $pipeline})
            OPTIONAL MATCH (service:Service)-[:DEPENDS_ON*1..4]->(p)
            WITH p, collect(DISTINCT service.id) + [$candidate] AS service_ids
            MATCH (decision:Decision)-[:GOVERNS]->(target)
            WHERE (target = p OR target.id IN service_ids)
              AND NOT (decision)<-[:SUPERSEDES]-()
            RETURN DISTINCT decision.id ORDER BY decision.id
        """, {"pipeline": pipeline_id, "candidate": candidate_id}).result_set
        return [row[0] for row in rows]

    def record_rejected_episode(self, pipeline_id, playbook_id, sim_report):
        """Record one RiskAuditor rejection and multiply its context weight by .8.

        The shadow graph/playbook pair is an idempotency key. Context nodes hold
        independent weights; Playbook.success_rate projects the last learned
        context for graph inspection, while recall always resolves the context.
        """
        if sim_report.get("simulation_approved") is not False:
            raise ValueError("Only rejected simulations can create a RejectedEpisode")
        decisions = sorted(set(sim_report["governing_decisions"]))
        if not decisions:
            raise ValueError("A rejected episode requires governing policy evidence")
        context = self._policy_context(pipeline_id, decisions)
        context_id = f"{playbook_id}:{context}"
        episode_id = "rejected_" + hashlib.sha256(
            f"{sim_report['shadow_graph_id']}:{playbook_id}".encode()
        ).hexdigest()[:24]
        token = uuid.uuid4().hex
        rows = self.graph.query("""
            MATCH (auditor:Agent {id: 'agent_auditor'})
            MATCH (p:DataPipeline {id: $pipeline})<-[:APPLIES_TO]-(pb:Playbook {id: $playbook})
            WHERE pb.target_route = $target
            MERGE (context:PlaybookContext {id: $context_id})
            ON CREATE SET context.policy_context = $context, context.pipeline_id = $pipeline,
                context.decision_ids = $decisions, context.success_rate = coalesce(pb.baseline_success_rate, pb.success_rate),
                context.runs = coalesce(pb.baseline_runs, pb.runs), context.rejections = 0
            MERGE (ep:RejectedEpisode {id: $episode})
            ON CREATE SET ep.token = $token, ep.timestamp = $ts, ep.outcome = 'REJECTED',
                ep.shadow_graph_id = $shadow, ep.incident_id = $incident,
                ep.policy_context = $context, ep.chosen_playbook = $playbook,
                ep.rejection_reasons = $reasons, ep.governing_decisions = $decisions,
                ep.policy_violation = $policy_violation, ep.sla_passed = $sla_passed,
                ep.capacity_passed = $capacity_passed, ep.penalty_decay = 0.8
            WITH auditor, p, pb, context, ep WHERE ep.token = $token
            WITH auditor, p, pb, context, ep,
                 context.success_rate AS previous_weight,
                 context.success_rate * 0.8 AS next_weight,
                 context.runs + 1 AS next_runs,
                 coalesce(p.memory_sequence, 0) + 1 AS next_sequence
            SET pb.baseline_success_rate = coalesce(pb.baseline_success_rate, pb.success_rate),
                pb.baseline_runs = coalesce(pb.baseline_runs, pb.runs)
            MERGE (pb)-[:HAS_CONTEXT]->(context)
            CREATE (auditor)-[:RECORDED]->(ep)-[:USED_PLAYBOOK]->(pb)
            CREATE (ep)-[:IN_CONTEXT]->(context)
            CREATE (event:PlaybookWeightEvent {
                id: $episode, playbook_id: $playbook, pipeline_id: $pipeline,
                policy_context: $context, decision_ids: $decisions,
                incident_id: $incident, timestamp: $ts, outcome: 'REJECTED',
                previous_weight: previous_weight, shadow_graph_id: $shadow
            })
            SET context.success_rate = next_weight,
                context.runs = next_runs, context.rejections = context.rejections + 1
            SET pb.success_rate = next_weight, pb.runs = pb.runs + 1,
                p.memory_sequence = next_sequence
            SET event.weight = next_weight, event.sequence = next_sequence
            CREATE (pb)-[:WEIGHT_UPDATED]->(event)
            CREATE (event)-[:FROM_EPISODE]->(ep)
            WITH context, ep, event
            UNWIND $decisions AS decision_id
            MATCH (decision:Decision {id: decision_id})
            MERGE (context)-[:UNDER_POLICY]->(decision)
            CREATE (ep)-[:EVALUATED_UNDER]->(decision)
            RETURN DISTINCT ep.id, event.previous_weight, event.weight, event.sequence
        """, {
            "pipeline": pipeline_id, "playbook": playbook_id, "target": sim_report["target_service"],
            "context_id": context_id, "context": context, "decisions": decisions,
            "episode": episode_id, "token": token, "ts": int(time.time()),
            "shadow": sim_report["shadow_graph_id"], "incident": sim_report["incident_id"],
            "reasons": sim_report.get("rejection_reasons", []),
            "policy_violation": sim_report["policy_violation"], "sla_passed": sim_report["sla_passed"],
            "capacity_passed": sim_report["capacity_passed"],
        }).result_set
        if not rows:
            rows = self.graph.ro_query("""
                MATCH (event:PlaybookWeightEvent {id: $id})
                RETURN event.id, event.previous_weight, event.weight, event.sequence
            """, {"id": episode_id}).result_set
        if not rows:
            raise ValueError("Playbook, pipeline or RiskAuditor is missing")
        return {"rejected_episode_id": rows[0][0], "playbook_id": playbook_id,
                "previous_weight": rows[0][1], "weight": rows[0][2],
                "sequence": rows[0][3], "policy_context": context}

    def get_playbook_weight_history(self, pipeline_id="pipeline_eu_payments"):
        """Persistent seed baselines and ordered weight updates for both routes."""
        baseline = self.graph.ro_query("""
            MATCH (pb:Playbook)-[:APPLIES_TO]->(:DataPipeline {id: $pipeline})
            RETURN pb.id, coalesce(pb.baseline_success_rate, pb.success_rate)
            ORDER BY pb.id
        """, {"pipeline": pipeline_id}).result_set
        history = [{"sequence": 0, "incident_id": "baseline", "playbook_id": row[0],
                    "weight": row[1], "policy_context": "baseline", "outcome": "BASELINE"}
                   for row in baseline]
        events = self.graph.ro_query("""
            MATCH (pb:Playbook)-[:WEIGHT_UPDATED]->(event:PlaybookWeightEvent)
            WHERE event.pipeline_id = $pipeline
            RETURN event.sequence, event.incident_id, pb.id, event.weight,
                   event.policy_context, event.outcome, event.decision_ids, event.timestamp
            ORDER BY event.sequence, pb.id
        """, {"pipeline": pipeline_id}).result_set
        keys = ["sequence", "incident_id", "playbook_id", "weight", "policy_context", "outcome", "decision_ids", "timestamp"]
        history.extend(dict(zip(keys, row)) for row in events)
        return history

    def inject_chaos_scenario(self, scenario: str) -> Dict[str, Any]:
        """Apply an idempotent failure injection to the current graph.

        Scenarios accumulate; callers can seed first for an isolated demo.
        Policy decisions, ownership and existing SLA contracts are preserved.
        """
        if scenario not in self.CHAOS_SCENARIOS:
            raise ValueError(f"Unknown chaos scenario: {scenario}")
        required = self.graph.query("""
            MATCH (p:DataPipeline {id: 'pipeline_eu_payments'})
            MATCH (ledger:Service {id: 'svc_core_ledger'})
            MATCH (checkout:Service {id: 'svc_merchant_checkout'})
            MATCH (dr:Service {id: 'svc_eu_zurich_dr'})
            MATCH (us:Region {id: 'us-east-1'})
            RETURN p.id
        """).result_set
        if not required:
            raise ValueError("Seed the Aegis topology before injecting chaos")
        queries = {
            "ledger_lock": """
                MATCH (ledger:Service {id: 'svc_core_ledger'})
                MATCH (us:Region {id: 'us-east-1'})
                SET ledger.status = 'LOCKED', ledger.lock_wait_ms = 6000,
                    ledger.current_load_pct = 99
                MERGE (replica:Service {id: 'svc_us_ledger_replica'})
                SET replica.name = 'US Settlement Replica', replica.region = 'us-east-1',
                    replica.status = 'LOCK_WAIT', replica.lock_wait_ms = 4000,
                    replica.current_load_pct = 96, replica.max_throughput_tps = 20000,
                    replica.base_latency_ms = 60
                MERGE (api:Service {id: 'svc_us_settlement_api'})
                SET api.name = 'US Settlement API', api.region = 'us-east-1',
                    api.status = 'AT_RISK', api.current_load_pct = 80,
                    api.max_throughput_tps = 20000, api.base_latency_ms = 40
                MERGE (api)-[:DEPENDS_ON]->(replica)
                MERGE (replica)-[:DEPENDS_ON]->(ledger)
                MERGE (replica)-[:LOCATED_IN]->(us)
                MERGE (api)-[:LOCATED_IN]->(us)
                MERGE (sla:SLA {id: 'sla_us_settlement'})
                SET sla.max_allowed_latency_ms = 400, sla.penalty_per_min_usd = 20000
                MERGE (api)-[:BOUND_BY]->(sla)
                WITH ledger
                MATCH (dependent:Service)-[:DEPENDS_ON*1..4]->(ledger)
                SET dependent.cascade_source = ledger.id
            """,
            "throughput_spike": """
                MATCH (p:DataPipeline {id: 'pipeline_eu_payments'})
                MATCH (dr:Service {id: 'svc_eu_zurich_dr'})
                SET p.throughput_tps = 18000, p.status = 'THROUGHPUT_SPIKE',
                    p.latency_ms = 2200, dr.current_load_pct = 95, dr.status = 'OVERLOADED'
            """,
            "sla_upgrade": """
                MATCH (checkout:Service {id: 'svc_merchant_checkout'})
                MERGE (platinum:SLA {id: 'sla_platinum_upgrade'})
                SET platinum.client_tier = 'Platinum FinTech',
                    platinum.max_allowed_latency_ms = 100,
                    platinum.min_throughput_tps = 10000,
                    platinum.penalty_per_min_usd = 75000
                MERGE (checkout)-[:BOUND_BY]->(platinum)
            """,
        }
        self.graph.query(queries[scenario])
        self.graph.query("""
            MATCH (p:DataPipeline {id: 'pipeline_eu_payments'})
            SET p.chaos_scenario = $scenario
        """, {"scenario": scenario})
        return {"scenario": scenario, "description": self.CHAOS_SCENARIOS[scenario]}

    def run_shadow_graph_simulation(
        self, pipeline_id: str, candidate_service_id: str, *,
        playbook_id: str = None, incident_id: str = None,
        record_rejection: bool = True,
    ) -> Dict[str, Any]:
        """Verify all downstream dependency paths (up to four hops) in a sandbox.

        Capacity model: 90% is the safe ceiling. Candidate background load plus
        the incoming pipeline TPS is projected against its throughput capacity.
        Existing dependents already carry the pipeline, so their projected load
        is max(current load, demand / capacity). Concurrent SLA minimum TPS
        obligations increase demand; every attached SLA must pass.
        After sandbox cleanup, rejected results automatically update RiskAuditor
        memory for the specified playbook, or matching route playbooks if omitted.
        Group related attempts with the same incident_id for historical charts.
        """
        verified_inputs = self._simulation_inputs()
        params = {"pipeline_id": pipeline_id, "cand_id": candidate_service_id}
        live = self.graph.query("""
            MATCH (p:DataPipeline {id: $pipeline_id})-[:CANDIDATE_FAILOVER]->
                  (cand:Service {id: $cand_id})-[:LOCATED_IN]->(reg:Region)
            OPTIONAL MATCH (dec:Decision)-[:GOVERNS]->(p)
            WHERE NOT (dec)<-[:SUPERSEDES]-()
            OPTIONAL MATCH (p)-[route:ROUTES_THROUGH {active: true}]->(cand)
            RETURN properties(p), properties(cand), properties(reg), properties(dec),
                   route IS NOT NULL, route.load_contribution_pct
        """, params).result_set
        if not live or not live[0][3]:
            raise ValueError("Candidate route or governing policy is missing")
        pipeline, candidate, region = live[0][:3]
        policies = {row[3]["id"]: row[3] for row in live if row[3]}
        candidate_is_active = bool(live[0][4])
        existing_contribution = live[0][5]
        topology_query = """
            MATCH path=(downstream:Service)-[:DEPENDS_ON*1..4]->
                       (p:DataPipeline {id: $pipeline_id})
            OPTIONAL MATCH (downstream)-[:BOUND_BY]->(sla:SLA)
            RETURN [n IN nodes(path) | properties(n)],
                   [r IN relationships(path) | properties(r)], properties(sla)
        """
        paths = self.graph.query(topology_query, {"pipeline_id": pipeline_id}).result_set
        if not paths or not any(row[2] for row in paths):
            raise ValueError("No downstream SLA dependency paths found")
        governed_ids = {pipeline_id, candidate_service_id}
        for path, _, _ in paths:
            governed_ids.update(service["id"] for service in path)
        policy_targets = {policy_id: pipeline_id for policy_id in policies}
        for policy, target_id in self.graph.ro_query("""
            MATCH (decision:Decision)-[:GOVERNS]->(target)
            WHERE target.id IN $ids AND NOT (decision)<-[:SUPERSEDES]-()
            RETURN properties(decision), target.id
        """, {"ids": sorted(governed_ids)}).result_set:
            policies[policy["id"]] = policy
            policy_targets[policy["id"]] = target_id

        if self._simulation_inputs() != verified_inputs:
            raise ValueError("Simulation inputs changed while reading; run a new simulation")
        shadow_name = f"shadow_sim_{uuid.uuid4().hex[:8]}"
        shadow_graph = self.db.select_graph(shadow_name)
        try:
            # Create first so every later failure can safely delete the graph.
            shadow_graph.query("CREATE (p:DataPipeline) SET p = $props",
                               {"props": dict(pipeline, status="SIMULATED_REROUTE")})
            nodes = {pipeline["id"]}
            dependencies = set()
            bindings = set()

            def copy_node(label, props):
                # Labels below are code constants, never caller-supplied Cypher.
                if props["id"] not in nodes:
                    shadow_graph.query(f"CREATE (n:{label}) SET n = $props", {"props": props})
                    nodes.add(props["id"])

            def copy_edge(kind, source, target, props=None):
                shadow_graph.query(
                    f"MATCH (a {{id: $source}}), (b {{id: $target}}) "
                    f"CREATE (a)-[r:{kind}]->(b) SET r = $props",
                    {"source": source, "target": target, "props": props or {}},
                )

            copy_node("Service", candidate)
            copy_node("Region", region)
            copy_edge("ROUTES_THROUGH", pipeline["id"], candidate["id"], {"simulated": True})
            copy_edge("LOCATED_IN", candidate["id"], region["id"])
            for path, relationships, sla in paths:
                for service in path[:-1]:
                    copy_node("Service", service)
                for index, props in enumerate(relationships):
                    pair = (path[index]["id"], path[index + 1]["id"])
                    if pair not in dependencies:
                        copy_edge("DEPENDS_ON", *pair, props)
                        dependencies.add(pair)
                if sla:
                    copy_node("SLA", sla)
                    binding = (path[0]["id"], sla["id"])
                    if binding not in bindings:
                        copy_edge("BOUND_BY", *binding)
                        bindings.add(binding)
            for policy in policies.values():
                copy_node("Decision", policy)
                copy_edge("GOVERNS", policy["id"], policy_targets[policy["id"]])

            # Verification reads the mutated shadow topology, not production.
            shadow_paths = shadow_graph.query(
                topology_query, {"pipeline_id": pipeline_id}
            ).result_set
            if not shadow_paths:
                raise ValueError("Shadow dependency paths are missing")
            demand = max(
                [float(pipeline["throughput_tps"])] +
                [float(row[2].get("min_throughput_tps", 0)) for row in shadow_paths if row[2]]
            )
            services = {candidate["id"]: candidate}
            for path, _, _ in shadow_paths:
                services.update((service["id"], service) for service in path[:-1])
            capacity_checks = []
            for service_id, service in sorted(services.items()):
                capacity = service.get("max_throughput_tps")
                load = service.get("current_load_pct")
                known = capacity is not None and capacity > 0 and load is not None
                projected = None
                if known:
                    traffic_load = demand / capacity * 100
                    if service_id == candidate["id"]:
                        previous = (existing_contribution if existing_contribution is not None
                                    else min(load, traffic_load)) if candidate_is_active else 0
                        projected = max(0, load - previous) + traffic_load
                    else:
                        projected = max(load, traffic_load)
                locked = service.get("status") in {"LOCKED", "LOCK_WAIT"}
                passed = known and projected <= 90 and not locked
                check = {
                    "service_id": service_id, "region": service.get("region"),
                    "current_load_pct": load, "projected_load_pct": projected,
                    "max_throughput_tps": capacity, "required_throughput_tps": demand,
                    "capacity_passed": bool(passed), "lock_blocked": locked,
                    "reason": ("missing capacity data" if not known else
                               "database lock" if locked else
                               "projected load exceeds 90%" if projected > 90 else "within capacity"),
                }
                capacity_checks.append(check)
                shadow_graph.query("""
                    MATCH (s:Service {id: $id})
                    SET s.projected_load_pct = $load, s.capacity_passed = $passed
                """, {"id": service_id, "load": projected, "passed": bool(passed)})

            sla_checks = []
            path_checks = []
            for path, _, sla in shadow_paths:
                latency = candidate["base_latency_ms"] + sum(
                    service.get("base_latency_ms", 0) + service.get("lock_wait_ms", 0)
                    for service in path[:-1]
                )
                path_ids = [service["id"] for service in path]
                path_checks.append({"service_path": path_ids, "projected_e2e_latency_ms": latency})
                if sla:
                    sla_checks.append({
                        "sla_id": sla["id"], "service_path": path_ids,
                        "projected_e2e_latency_ms": latency,
                        "sla_limit_ms": sla["max_allowed_latency_ms"],
                        "min_throughput_tps": sla.get("min_throughput_tps", 0),
                        "penalty_at_risk_usd": sla.get("penalty_per_min_usd", 0),
                        "sla_passed": latency <= sla["max_allowed_latency_ms"],
                    })
            if not sla_checks:
                raise ValueError("Shadow SLA evidence is missing")
            capacity_passed = all(check["capacity_passed"] for check in capacity_checks)
            sla_passed = all(check["sla_passed"] for check in sla_checks)
            regional_violation = any(policy.get("forbids_non_eu", False) for policy in policies.values()) and not region["gdpr_compliant"]
            frozen_policies = [policy for policy in policies.values() if policy.get("traffic_blocked") is True]
            violation = regional_violation or bool(frozen_policies)
            reasons = []
            if regional_violation:
                reasons.append("Non-EU route violates governing policy")
            if frozen_policies:
                reasons.append("Traffic frozen by Slack directive for: " + ", ".join(
                    sorted({policy_targets[policy["id"]] for policy in frozen_policies})
                ))
            if not capacity_passed:
                reasons.append("Multi-hop capacity or database lock constraint failed")
            if not sla_passed:
                reasons.append("One or more downstream SLA latency limits failed")
            # Keep legacy summary fields; detailed per-path evidence is authoritative.
            contracts = {check["sla_id"]: check for check in sla_checks}
            report = {
                "verified_inputs": verified_inputs,
                "incident_id": incident_id or f"incident_{uuid.uuid4().hex}",
                "pipeline_id": pipeline_id,
                "shadow_graph_id": shadow_name, "target_service": candidate["id"],
                "target_name": candidate.get("name", candidate["id"]), "region": region["id"],
                "projected_e2e_latency_ms": max(check["projected_e2e_latency_ms"] for check in sla_checks),
                "sla_limit_ms": min(check["sla_limit_ms"] for check in sla_checks),
                "sla_passed": sla_passed, "policy_violation": bool(violation),
                "governing_decision": sorted(policy["id"] for policy in frozen_policies)[0] if frozen_policies else sorted(policies)[0],
                "governing_decisions": sorted(policies),
                "traffic_freeze_violation": bool(frozen_policies),
                "policy_checks": [{
                    "decision_id": policy_id, "target_id": policy_targets[policy_id],
                    "summary": policy.get("summary"), "source_url": policy.get("source_url"),
                    "author_handle": policy.get("author_handle"),
                    "traffic_blocked": policy.get("traffic_blocked", False),
                    "forbids_non_eu": policy.get("forbids_non_eu", False),
                } for policy_id, policy in sorted(policies.items())],
                "penalty_at_risk_usd": sum(check["penalty_at_risk_usd"] for check in contracts.values()),
                "required_throughput_tps": demand, "capacity_passed": capacity_passed,
                "capacity_checks": capacity_checks, "sla_checks": sla_checks,
                "dependency_paths": path_checks, "rejection_reasons": reasons,
                "simulation_approved": sla_passed and capacity_passed and not violation,
                "chaos_scenario": pipeline.get("chaos_scenario"),
                "observed_pipeline_latency_ms": pipeline.get("latency_ms", 0),
            }
            node_rows = shadow_graph.query("""
                MATCH (n) RETURN id(n), labels(n), properties(n) ORDER BY id(n)
            """).result_set
            edge_rows = shadow_graph.query("""
                MATCH (source)-[edge]->(target)
                RETURN id(edge), id(source), id(target), type(edge), properties(edge)
                ORDER BY id(edge)
            """).result_set
            snapshot = {
                "graph_id": shadow_name,
                "nodes": [{"id": str(row[0]), "labels": row[1], "properties": row[2]}
                          for row in node_rows],
                "edges": [{"id": str(row[0]), "source": str(row[1]), "target": str(row[2]),
                           "type": row[3], "properties": row[4]} for row in edge_rows],
            }
            # Materialize JSON before deleting the sandbox; no SDK entities escape.
            report["shadow_snapshot"] = json.loads(json.dumps(snapshot))
        finally:
            shadow_graph.delete()
        report["rejection_memory"] = []
        if report["simulation_approved"] is False and record_rejection:
            playbooks = self.graph.ro_query("""
                MATCH (pb:Playbook)-[:APPLIES_TO]->(:DataPipeline {id: $pipeline})
                WHERE pb.target_route = $target AND ($playbook IS NULL OR pb.id = $playbook)
                RETURN pb.id ORDER BY pb.id
            """, {"pipeline": pipeline_id, "target": candidate_service_id,
                  "playbook": playbook_id}).result_set
            if playbook_id and not playbooks:
                raise ValueError("Playbook does not apply to the simulated pipeline/route")
            for row in playbooks:
                report["rejection_memory"].append(self.record_rejected_episode(pipeline_id, row[0], report))
        return report

    def coordinate_handoff_and_commit(
        self,
        pipeline_id: str,
        playbook_id: str,
        sim_report: Dict[str, Any],
        expert_handle: str,
        policy_source: str,
        *,
        observed_latency_ms: float = None,
        recovery_action_id: str = None,
        recovery_action_version: int = None,
    ) -> Dict[str, Any]:
        """
        Track 2 Multi-Agent Coordination & Procedural Memory Loop.
        """
        if not sim_report.get("simulation_approved"):
            raise ValueError("Cannot commit a rejected shadow simulation")
        if sim_report.get("pipeline_id") != pipeline_id:
            raise ValueError("Simulation report belongs to a different pipeline")
        if observed_latency_ms is not None:
            if (isinstance(observed_latency_ms, bool)
                    or not isinstance(observed_latency_ms, (int, float))
                    or not math.isfinite(observed_latency_ms) or observed_latency_ms < 0
                    or observed_latency_ms > sim_report["sla_limit_ms"]
                    or not recovery_action_id
                    or isinstance(recovery_action_version, bool)
                    or not isinstance(recovery_action_version, int) or recovery_action_version < 1):
                raise ValueError("Observed latency must be finite, within the SLA and bound to a recovery action")
        elif recovery_action_id is not None:
            raise ValueError("A recovery action requires an observed latency")
        commit_key = hashlib.sha256(json.dumps(
            [pipeline_id, playbook_id, sim_report["shadow_graph_id"]],
        ).encode()).hexdigest()
        lock_id = f"lock_{commit_key}"
        episode_id = f"ep_{commit_key}"
        decisions = sorted(set(sim_report["governing_decisions"]))
        context_key = self._policy_context(pipeline_id, decisions)

        if not sim_report.get("verified_inputs"):
            raise ValueError("Simulation input evidence missing; run a new simulation")
        action_guard = """
        MATCH (recovery:RecoveryAction {id: $recovery_id})
        WHERE recovery.status = 'FINALIZING' AND recovery.version = $recovery_version
        WITH recovery
        """ if recovery_action_id else ""
        commit_cypher = action_guard + "CALL { " + self._INPUT_STATE + " RETURN " + self._INPUT_FRESH + """ AS fresh
        }
        WITH fresh WHERE fresh
        MATCH (planner:Agent {id: 'agent_planner'})
        MATCH (auditor:Agent {id: 'agent_auditor'})
        MATCH (executor:Agent {id: 'agent_executor'})
        MATCH (p:DataPipeline {id: $pipeline_id})-[old_route:ROUTES_THROUGH {active: true}]->(old_svc:Service)
        MATCH (new_svc:Service {id: $target_service})
        MATCH (pb:Playbook {id: $playbook_id})-[:APPLIES_TO]->(p)
        WHERE pb.target_route = $target_service
        OPTIONAL MATCH (existing:PlaybookWeightEvent {id: $episode_id})
        WITH planner, auditor, executor, p, old_route, old_svc, new_svc, pb, existing
        WHERE existing IS NULL
        OPTIONAL MATCH (dependent:Service)-[:DEPENDS_ON*1..4]->(p)
        WITH planner, auditor, executor, p, old_route, old_svc, new_svc, pb,
             collect(DISTINCT dependent.id) + [$target_service] AS governed_services
        OPTIONAL MATCH (active_policy:Decision)-[:GOVERNS]->(governed)
        WHERE (governed = p OR governed.id IN governed_services)
          AND NOT (active_policy)<-[:SUPERSEDES]-()
        WITH planner, auditor, executor, p, old_route, old_svc, new_svc, pb,
             collect(DISTINCT active_policy.id) AS active_decisions
        WHERE size(active_decisions) = size($decisions)
          AND all(decision_id IN active_decisions WHERE decision_id IN $decisions)
        MERGE (context:PlaybookContext {id: $context_id})
        ON CREATE SET context.policy_context = $context, context.pipeline_id = $pipeline_id,
            context.decision_ids = $decisions,
            context.success_rate = coalesce(pb.baseline_success_rate, pb.success_rate),
            context.runs = coalesce(pb.baseline_runs, pb.runs), context.rejections = 0
        SET pb.baseline_success_rate = coalesce(pb.baseline_success_rate, pb.success_rate),
            pb.baseline_runs = coalesce(pb.baseline_runs, pb.runs)
        MERGE (pb)-[:HAS_CONTEXT]->(context)

        WITH planner, auditor, executor, p, old_route, old_svc, new_svc, pb, context,
             context.success_rate AS previous_weight,
             (context.success_rate * context.runs + 1.0) / (context.runs + 1) AS next_weight,
             context.runs + 1 AS next_runs,
             coalesce(p.memory_sequence, 0) + 1 AS next_sequence,
             coalesce(old_route.load_contribution_pct,
                 CASE WHEN old_svc.max_throughput_tps > 0
                 THEN p.throughput_tps * 100.0 / old_svc.max_throughput_tps ELSE 0 END) AS old_contribution

        CREATE (lock:HandoffLock {
            id: $lock_id,
            shadow_graph_verified: $shadow_id,
            projected_latency_ms: $proj_lat,
            status: 'APPROVED_BY_AUDITOR',
            timestamp: $ts
        })
        CREATE (planner)-[:INITIATED_HANDOFF]->(lock)<-[:VERIFIED_HANDOFF]-(auditor)
        CREATE (lock)-[:AUTHORIZED_EXECUTION]->(executor)

        // Older graphs have no per-route contribution. Infer it once using
        // the same throughput/capacity model, then retain it on the old route.
        SET old_route.load_contribution_pct = old_contribution
        SET old_svc.current_load_pct = CASE
            WHEN old_svc.current_load_pct >= old_contribution
            THEN old_svc.current_load_pct - old_contribution
            ELSE 0 END
        SET old_route.active = false
        CREATE (p)-[:ROUTES_THROUGH {active: true, switched_at: $ts, authorized_by: $lock_id,
                                    load_contribution_pct: $traffic_load}]->(new_svc)
        SET p.status = 'HEALTHY', p.latency_ms = $proj_lat,
            p.latency_source = 'model_projection', p.observed_gateway_p95_ms = $observed_latency
        SET new_svc.current_load_pct = coalesce($target_load, new_svc.current_load_pct)

        CREATE (ep:Episode {
            id: $episode_id,
            trigger_event: $trigger_event,
            chosen_playbook: $playbook_id,
            outcome: 'SUCCESS',
            evidence_type: $latency_source, recovery_action_id: $recovery_id,
            observed_latency_ms: $observed_latency,
            latency_saved_ms: ($previous_latency - $proj_lat),
            notified_owner: $expert_handle,
            incident_id: $incident, policy_context: $context,
            timestamp: $ts
        })
        CREATE (executor)-[:RECORDED]->(ep)-[:USED_PLAYBOOK]->(pb)
        CREATE (ep)-[:IN_CONTEXT]->(context)
        CREATE (event:PlaybookWeightEvent {
            id: $episode_id, playbook_id: $playbook_id, pipeline_id: $pipeline_id,
            policy_context: $context, decision_ids: $decisions,
            incident_id: $incident, timestamp: $ts, outcome: 'SUCCESS',
            previous_weight: previous_weight, shadow_graph_id: $shadow_id,
            handoff_lock_id: $lock_id, disconnected_service: old_svc.id,
            connected_service: new_svc.id, notified_expert: $expert_handle,
            policy_citation: $policy_source,
            recovery_action_id: $recovery_id, observed_latency_ms: $observed_latency,
            evidence_type: $latency_source
        })
        SET context.success_rate = next_weight, context.runs = next_runs
        SET pb.success_rate = next_weight, pb.runs = pb.runs + 1,
            p.memory_sequence = next_sequence
        SET event.weight = next_weight, event.sequence = next_sequence
        CREATE (pb)-[:WEIGHT_UPDATED]->(event)-[:FROM_EPISODE]->(ep)

        WITH lock, old_svc, new_svc, pb, ep, context, next_weight
        UNWIND $decisions AS decision_id
        MATCH (decision:Decision {id: decision_id})
        MERGE (context)-[:UNDER_POLICY]->(decision)
        CREATE (ep)-[:EVALUATED_UNDER]->(decision)
        RETURN DISTINCT lock.id, old_svc.id, new_svc.id, next_weight, ep.id,
                        $expert_handle, $policy_source, $context
        """
        res = self.graph.query(
            commit_cypher,
            {
                "input_nodes": sim_report["verified_inputs"]["nodes"],
                "input_edges": sim_report["verified_inputs"]["edges"],
                "canonical_nodes": sim_report["verified_inputs"].get("canonical_nodes"),
                "canonical_edges": sim_report["verified_inputs"].get("canonical_edges"),
                "traffic_load": next(check["required_throughput_tps"] / check["max_throughput_tps"] * 100
                                     for check in sim_report["capacity_checks"]
                                     if check["service_id"] == sim_report["target_service"]),
                "pipeline_id": pipeline_id,
                "target_service": sim_report["target_service"],
                "playbook_id": playbook_id,
                "context": context_key, "context_id": f"{playbook_id}:{context_key}",
                "decisions": decisions,
                "incident": sim_report.get("incident_id", sim_report["shadow_graph_id"]),
                "lock_id": lock_id,
                "shadow_id": sim_report["shadow_graph_id"],
                "proj_lat": sim_report["projected_e2e_latency_ms"],
                "observed_latency": observed_latency_ms,
                "latency_source": "loopback_http" if observed_latency_ms is not None else "model_projection",
                "recovery_id": recovery_action_id,
                "recovery_version": recovery_action_version,
                "target_load": next((check["projected_load_pct"]
                                     for check in sim_report.get("capacity_checks", [])
                                     if check["service_id"] == sim_report["target_service"]), None),
                "previous_latency": sim_report.get("observed_pipeline_latency_ms", 1420),
                "trigger_event": f"{pipeline_id}: {sim_report.get('chaos_scenario') or 'latency incident'}",
                "expert_handle": expert_handle,
                "policy_source": policy_source,
                "episode_id": episode_id,
                "ts": int(time.time()),
            },
        ).result_set
        if not res:
            # A retry returns the original receipt, even if policies have since
            # changed. The write query above guards both replay and policy drift
            # atomically, before any route or memory mutation.
            res = self.graph.ro_query("""
                MATCH (event:PlaybookWeightEvent {id: $id})
                RETURN event.handoff_lock_id, event.disconnected_service,
                       event.connected_service, event.weight, event.id,
                       event.notified_expert, event.policy_citation, event.policy_context
            """, {"id": episode_id}).result_set
        if not res:
            raise ValueError("Commit rejected: policy context changed, simulation inputs changed, or commit prerequisites are missing; run a new simulation")
        res = res[0]

        return {
            "handoff_lock_id": res[0],
            "disconnected_service": res[1],
            "connected_service": res[2],
            "updated_playbook_success_rate": round(res[3], 4),
            "recorded_episode_id": res[4],
            "notified_expert": res[5],
            "policy_citation": res[6],
            "policy_context": res[7],
        }


if __name__ == "__main__":
    engine = AegisGraphEngine(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", 6379)),
    )

    engine.seed_tri_layer_universe()

    target_pipeline = "pipeline_eu_payments"
    print(f"[!] INCIDENT TRIGGERED: '{target_pipeline}' latency spiked to 1420ms (SLA Limit: 250ms)")

    candidates = engine.recall_playbooks_and_company_brain(target_pipeline)
    print(f"[*] Retrieved {len(candidates)} candidate playbooks from Agent Procedural Memory:\n")

    for idx, cand in enumerate(candidates, 1):
        print(
            f"  Candidate #{idx}: {cand['playbook_name']} (Historical Success: {cand['historical_success_rate']*100:.1f}%)"
        )
        print(
            f"    -> Active Company Brain Policy [{cand['active_decision_id']}] (Supersedes {cand['superseded_decision_id']}):"
        )
        print(f"       \"{cand['active_policy_summary']}\"")

        sim_report = engine.run_shadow_graph_simulation(
            pipeline_id=target_pipeline,
            candidate_service_id=cand["candidate_service_id"],
        )

        if not sim_report["simulation_approved"]:
            print(
                f"    [X] SHADOW SIMULATION REJECTED ({sim_report['target_name']} in {sim_report['region']}):"
            )
            print(
                f"        Policy Violation: {sim_report['policy_violation']} (Governed by {sim_report['governing_decision']})\n"
            )
        else:
            print(
                f"    [✓] SHADOW SIMULATION PASSED ({sim_report['target_name']} in {sim_report['region']}):"
            )
            print(
                f"        Projected E2E Latency: {sim_report['projected_e2e_latency_ms']}ms <= SLA {sim_report['sla_limit_ms']}ms | GDPR Compliant: True\n"
            )

            outcome = engine.coordinate_handoff_and_commit(
                pipeline_id=target_pipeline,
                playbook_id=cand["playbook_id"],
                sim_report=sim_report,
                expert_handle=cand["expert_handle"],
                policy_source=cand["policy_source"],
            )
            print("[+] MULTI-AGENT EXECUTION & TRACEABLE AUDIT LOG:")
            print(f"    - Handoff Lock Verified: {outcome['handoff_lock_id']}")
            print(
                f"    - Rerouted Traffic: {outcome['disconnected_service']} -> {outcome['connected_service']}"
            )
            print(
                f"    - Updated Playbook Success Rate: {outcome['updated_playbook_success_rate']*100:.2f}%"
            )
            print(
                f"    - Domain Owner Notified: {outcome['notified_expert']} (Citation: {outcome['policy_citation']})"
            )
            break
