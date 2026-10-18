"""Graph-backed proposal, independent audit and resumable local execution phases."""

import json
import time
import uuid
from urllib.error import URLError

from aegis_runtime import RuntimeControlError


PIPELINE = "pipeline_eu_payments"
TERMINAL = {"VERIFIED", "REJECTED", "FAILED", "STALE"}


class RecoveryBusy(ValueError):
    """A different executor currently owns this pipeline."""


class RecoveryCoordinator:
    def __init__(self, engine, runtime):
        self.engine, self.runtime = engine, runtime

    def get(self, action_id):
        rows = self.engine.graph.ro_query(
            "MATCH (a:RecoveryAction {id: $id}) RETURN a.document", {"id": action_id},
        ).result_set
        if not rows:
            raise ValueError("Recovery action not found")
        return json.loads(rows[0][0])

    def list_actions(self, limit=20):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Limit must be 1–100")
        return [json.loads(row[0]) for row in self.engine.graph.ro_query(
            "MATCH (a:RecoveryAction) RETURN a.document ORDER BY a.created_at DESC LIMIT $limit",
            {"limit": limit},
        ).result_set]

    @staticmethod
    def _encode(action):
        return json.dumps(action, sort_keys=True, allow_nan=False)

    def _save(self, action, actor="agent_executor"):
        previous = action["version"]
        updated = {**action, "version": previous + 1, "updated_at": time.time()}
        rows = self.engine.graph.query("""
            MATCH (a:RecoveryAction {id: $id}), (actor:Agent {id: $actor})
            WHERE a.version = $version
            SET a.status = $status, a.version = $next, a.document = $document
            MERGE (actor)-[:UPDATED_RECOVERY]->(a)
            WITH a
            OPTIONAL MATCH (lease:RecoveryLease {pipeline_id: $pipeline})
            FOREACH (item IN CASE WHEN $terminal AND lease.owner = $id THEN [lease] ELSE [] END |
                SET item.owner = '')
            RETURN a.id
        """, {"id": action["id"], "actor": actor, "pipeline": action["pipeline_id"],
                "version": previous, "next": previous + 1, "status": action["status"],
                "document": self._encode(updated), "terminal": action["status"] in TERMINAL}).result_set
        if not rows:
            latest = self.get(action["id"])
            if latest["status"] in TERMINAL:
                return latest
            raise RecoveryBusy("Action changed in another worker; read it again")
        return updated

    def _sla_limit(self, pipeline_id):
        if pipeline_id != PIPELINE:
            raise ValueError("This local simulator serves pipeline_eu_payments only")
        rows = self.engine.graph.ro_query("""
            MATCH (:DataPipeline {id: $id})<-[:DEPENDS_ON*1..4]-(:Service)-[:BOUND_BY]->(sla:SLA)
            RETURN min(sla.max_allowed_latency_ms)
        """, {"id": pipeline_id}).result_set
        if not rows or rows[0][0] is None:
            raise ValueError("Pipeline has no SLA; seed the graph first")
        return rows[0][0]

    def propose(self, pipeline_id, playbook_id):
        self._sla_limit(pipeline_id)
        candidates = self.engine.recall_playbooks_and_company_brain(pipeline_id)
        candidate = next((row for row in candidates if row["playbook_id"] == playbook_id), None)
        if not candidate:
            raise ValueError("Playbook does not apply to this pipeline")
        action = {"id": "recovery_" + uuid.uuid4().hex, "pipeline_id": pipeline_id,
                  "candidate": candidate, "status": "PROPOSED", "version": 1,
                  "runtime": self.runtime.snapshot(), "before": self.runtime.measure(),
                  "created_at": time.time(), "trace": [{"role": "Planner",
                  "tool": "recall_procedural_playbooks", "selected": playbook_id}]}
        rows = self.engine.graph.query("""
            MATCH (p:DataPipeline {id: $pipeline}), (planner:Agent {id: 'agent_planner'}),
                  (pb:Playbook {id: $playbook})-[:APPLIES_TO]->(p)
            CREATE (a:RecoveryAction {id: $id, status: 'PROPOSED', version: 1,
                                     created_at: $created, document: $document})
            CREATE (planner)-[:PROPOSED_RECOVERY]->(a)-[:USES_PLAYBOOK]->(pb)
            CREATE (a)-[:RECOVERS]->(p)
            RETURN a.id
        """, {"id": action["id"], "pipeline": pipeline_id, "playbook": playbook_id,
                "created": action["created_at"], "document": self._encode(action)}).result_set
        if not rows:
            raise ValueError("Missing planner, pipeline or playbook")
        return action

    def audit(self, action_id):
        action = self.get(action_id)
        if action["status"] != "PROPOSED":
            return action
        report = self.engine.run_shadow_graph_simulation(
            action["pipeline_id"], action["candidate"]["candidate_service_id"],
            playbook_id=action["candidate"]["playbook_id"], incident_id=action_id,
        )
        action["simulation"] = report
        action["status"] = "APPROVED" if report["simulation_approved"] else "REJECTED"
        action["trace"].append({"role": "RiskAuditor", "tool": "simulate_shadow_failover",
                                "approved": report["simulation_approved"],
                                "policy_decisions": report["governing_decisions"],
                                "reasons": report["rejection_reasons"]})
        return self._save(action, "agent_auditor")

    def claim(self, action_id):
        action = self.get(action_id)
        if action["status"] in TERMINAL:
            return action
        if action["status"] not in {"APPROVED", "EXECUTING", "FINALIZING"}:
            raise ValueError("An independent audit must approve this action first")
        updated = {**action, "status": "EXECUTING", "version": action["version"] + 1}
        inputs = action["simulation"]["verified_inputs"]
        query = "CALL { " + self.engine._INPUT_STATE + " RETURN " + self.engine._INPUT_FRESH + """ AS fresh
        }
        WITH fresh WHERE fresh
        MATCH (a:RecoveryAction {id: $id}), (executor:Agent {id: 'agent_executor'})
        WHERE a.version = $version AND a.status IN ['APPROVED', 'EXECUTING', 'FINALIZING']
        MERGE (lease:RecoveryLease {pipeline_id: $pipeline})
        ON CREATE SET lease.owner = ''
        WITH a, executor, lease WHERE lease.owner = '' OR lease.owner = $id
        SET lease.owner = $id, a.status = 'EXECUTING', a.version = $next, a.document = $document
        MERGE (executor)-[:CLAIMED_RECOVERY]->(a)
        RETURN a.id
        """
        rows = self.engine.graph.query(query, {
            "id": action_id, "pipeline": action["pipeline_id"], "version": action["version"],
            "next": updated["version"], "document": self._encode(updated),
            "input_nodes": inputs["nodes"], "input_edges": inputs["edges"],
            "canonical_nodes": inputs.get("canonical_nodes"), "canonical_edges": inputs.get("canonical_edges"),
        }).result_set
        if rows:
            return updated
        if self.engine._simulation_inputs() != inputs:
            return self._stop(action, "Graph policy, topology or capacity changed after audit", "STALE")
        raise RecoveryBusy("Pipeline execution is held by another action; resume its action ID")

    def _committed(self, action_id):
        rows = self.engine.graph.ro_query("""
            MATCH (event:PlaybookWeightEvent {recovery_action_id: $id})
            RETURN event.handoff_lock_id, event.disconnected_service, event.connected_service,
                   event.weight, event.id, event.notified_expert, event.policy_citation, event.policy_context
        """, {"id": action_id}).result_set
        if not rows:
            return None
        return dict(zip(["handoff_lock_id", "disconnected_service", "connected_service",
                         "updated_playbook_success_rate", "recorded_episode_id", "identified_owner",
                         "policy_citation", "policy_context"], rows[0]))

    def _stop(self, action, reason, status="FAILED"):
        committed = self._committed(action["id"])
        if committed:
            latest = self.get(action["id"])
            if latest["status"] in TERMINAL:
                return latest
            latest["status"], latest["outcome"] = "VERIFIED", committed
            return self._save(latest)
        action["status"], action["reason"], action["stop_status"] = "STOPPING", reason, status
        # Own failure terminalization before touching the router. A stale worker
        # cannot compensate after another worker has advanced or verified it.
        action = self._save(action)
        if action["status"] in TERMINAL:
            return action
        receipt = action.get("runtime_receipt")
        if receipt is None:
            try:
                if self.runtime.snapshot()["instance_id"] == action["runtime"]["instance_id"]:
                    receipt = self.runtime.receipt(action["id"])
                    if receipt:
                        action["runtime_receipt"] = receipt
            except (RuntimeControlError, URLError, OSError) as error:
                action["compensation_error"] = "Could not inspect runtime receipt: " + str(error)
        if receipt:
            try:
                action["compensation"] = self.runtime.switch(action["id"] + ":stop", None, receipt["revision"])
            except (RuntimeControlError, URLError, OSError) as error:
                action["compensation_error"] = str(error)
        if status == "FAILED" and action.get("observation"):
            failure = {**action["simulation"], "simulation_approved": False,
                       "rejection_reasons": ["Observed local HTTP recovery failed: " + reason]}
            action["failure_memory"] = self.engine.record_rejected_episode(
                action["pipeline_id"], action["candidate"]["playbook_id"], failure)
        action["status"] = status
        return self._save(action)

    def execute(self, action_id):
        action = self.get(action_id)
        if action["status"] in TERMINAL:
            return action
        committed = self._committed(action_id)
        if committed:
            action["status"], action["outcome"] = "VERIFIED", committed
            return self._save(action)
        if action["status"] == "STOPPING":
            return self._stop(action, action["reason"], action["stop_status"])
        action = self.claim(action_id)
        if action["status"] in TERMINAL:
            return action
        try:
            if self.runtime.snapshot()["instance_id"] != action["runtime"]["instance_id"]:
                return self._stop(action, "Approval belongs to a different local runtime", "STALE")
            receipt = self.runtime.switch(action_id, action["candidate"]["candidate_service_id"],
                                          action["runtime"]["revision"])
            action["runtime_receipt"] = receipt
            observation = self.runtime.measure()
            action["observation"] = observation
            action["trace"].append({"role": "Executor", "tool": "execute_local_route_and_measure",
                                    "receipt_revision": receipt["revision"], "errors": observation["errors"],
                                    "measured_p95_ms": observation["p95_ms"]})
            action = self._save(action)
            if action["status"] in TERMINAL:
                return action
            if self.engine._simulation_inputs() != action["simulation"]["verified_inputs"]:
                return self._stop(action, "Graph changed while the HTTP action was in flight", "STALE")
            current = self.runtime.snapshot()
            passed = (observation["errors"] == 0
                      and observation["p95_ms"] <= action["simulation"]["sla_limit_ms"]
                      and observation["service_ids"] == [receipt["target"]]
                      and all(row.get("revision") == receipt["revision"] for row in observation["samples"])
                      and current["revision"] == receipt["revision"] and current["target"] == receipt["target"])
            if not passed:
                return self._stop(action, "Observed requests failed the local latency, availability or routing check")
            action["status"] = "FINALIZING"
            action = self._save(action)
            if action["status"] in TERMINAL:
                return action
            outcome = self.engine.coordinate_handoff_and_commit(
                action["pipeline_id"], action["candidate"]["playbook_id"], action["simulation"],
                action["candidate"]["expert_handle"], action["candidate"]["policy_source"],
                observed_latency_ms=observation["p95_ms"], recovery_action_id=action_id,
                recovery_action_version=action["version"],
            )
            outcome["identified_owner"] = outcome.pop("notified_expert")
            action["status"], action["outcome"] = "VERIFIED", outcome
            return self._save(action)
        except RecoveryBusy:
            # Another worker owns the newer action version. It must be allowed
            # to finish; a lost compare-and-set is not a routing failure.
            raise
        except (RuntimeControlError, URLError, OSError, ValueError) as error:
            # A database commit may have succeeded before a response was lost.
            committed = self._committed(action_id)
            if committed:
                action["status"], action["outcome"] = "VERIFIED", committed
                return self._save(action)
            return self._stop(action, str(error))

    def triage(self, pipeline_id=PIPELINE, use_memory=True):
        limit = self._sla_limit(pipeline_id)
        before = self.runtime.measure()
        if not before["errors"] and before["p95_ms"] <= limit:
            return {"status": "ALREADY_HEALTHY", "before": before, "attempts": []}
        candidates = self.engine.recall_playbooks_and_company_brain(pipeline_id)
        if not use_memory:
            baselines = dict(self.engine.graph.ro_query(
                "MATCH (pb:Playbook)-[:APPLIES_TO]->(:DataPipeline {id: $id}) "
                "RETURN pb.id, coalesce(pb.baseline_success_rate, pb.success_rate)", {"id": pipeline_id},
            ).result_set)
            candidates.sort(key=lambda row: (-baselines[row["playbook_id"]], row["playbook_id"]))
        attempts = []
        for candidate in candidates:
            action = self.propose(pipeline_id, candidate["playbook_id"])
            action = self.audit(action["id"])
            if action["status"] == "APPROVED":
                action = self.execute(action["id"])
            attempts.append(action)
            if action["status"] != "REJECTED":
                return {"status": action["status"], "before": before, "attempts": attempts,
                        "use_memory": use_memory}
        return {"status": "BLOCKED", "before": before, "attempts": attempts, "use_memory": use_memory}
