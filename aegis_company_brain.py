"""Schema-grounded question planning and read-only Company Brain retrieval."""

from datetime import datetime, timedelta, timezone
import re
import uuid


SCHEMA_URI = "falkordb://aegis/schema"
EXAMPLES = [
    "Who owns services impacted by BaFin Audit #88?",
    "Which decisions were superseded today?",
    "What are the active policies?",
    "Who owns svc_merchant_checkout?",
    "What superseded DEC-2026-101?",
    "Which decisions did DEC-2026-409 supersede?",
]


def company_brain_schema(graph_name):
    return {
        "graph_name": graph_name,
        "layers": {
            "Layer_A_Company_Brain_Track3": {
                "nodes": ["Person", "Decision", "Policy"],
                "edges": ["AUTHORED", "SUPERSEDES", "GOVERNS", "OWNS"],
            },
            "Layer_B_Service_Topology_Track1": {
                "nodes": ["Region", "DataPipeline", "Service", "SLA"],
                "edges": ["ROUTES_THROUGH", "CANDIDATE_FAILOVER", "LOCATED_IN", "DEPENDS_ON", "BOUND_BY"],
            },
            "Layer_C_Agent_Memory_Track2": {
                "nodes": ["Agent", "Playbook", "Episode", "RejectedEpisode", "PlaybookContext", "PlaybookWeightEvent", "HandoffLock"],
                "edges": ["RECORDED", "USED_PLAYBOOK", "APPLIES_TO", "INITIATED_HANDOFF",
                          "VERIFIED_HANDOFF", "AUTHORIZED_EXECUTION", "HAS_CONTEXT", "IN_CONTEXT",
                          "EVALUATED_UNDER", "UNDER_POLICY", "WEIGHT_UPDATED", "FROM_EPISODE"],
            },
        },
        "company_brain_properties": {
            "Person": ["id", "name", "slack_handle", "role"],
            "Decision": ["id", "summary", "timestamp", "active", "source_url", "forbids_non_eu",
                         "traffic_blocked", "target_id", "author_handle", "event_id"],
            "Service": ["id", "name", "region", "status"],
            "DataPipeline": ["id", "name", "status"],
        },
        "relationship_patterns": [
            "(Person)-[:OWNS]->(Service|DataPipeline|SLA)",
            "(Decision)-[:GOVERNS]->(Service|DataPipeline)",
            "(Decision)-[:SUPERSEDES]->(Decision)",
            "(Service)-[:DEPENDS_ON]->(Service|DataPipeline)",
        ],
        "query_contract": {
            "schema_uri": SCHEMA_URI, "examples": EXAMPLES,
            "translation": "Deterministic intent templates; no arbitrary Cypher input",
            "timezone": "UTC", "max_dependency_hops": 4, "max_rows": 100,
            "supersession_time": "timestamp of the superseding Decision (Unix seconds)",
            "active_decisions": "WHERE NOT (dec)<-[:SUPERSEDES]-()",
            "audit_inheritance": "Uninterrupted SUPERSEDES ancestry with forbids_non_eu=true; waivers break inheritance",
            "decision_lineage": "Explicit predecessor or successor questions; maximum four hops",
        },
    }


def _node(variable):
    return f"[id({variable}), labels({variable}), properties({variable})]"


def _edge(variable, source, target):
    return f"[id({variable}), id({source}), id({target}), type({variable}), properties({variable})]"


def _optional(expression, variable):
    return f"CASE WHEN {variable} IS NULL THEN [] ELSE [{expression}] END"


PATH_NODES = "[n IN nodes(path) | [id(n), labels(n), properties(n)]]"
PATH_EDGES = "[r IN relationships(path) | [id(r), id(startNode(r)), id(endNode(r)), type(r), properties(r)]]"


def plan_company_brain_question(question, *, limit=50, now=None):
    """Compile supported English intents to fixed, parameterized read queries."""
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise ValueError("Enter a question of 1–1000 characters.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Result limit must be an integer between 1 and 100.")
    text = question.strip().lower()
    if re.search(r"\b(create|merge|delete|detach|remove|drop|call|foreach|set)\b|;", text):
        raise ValueError("Ask a read-only question about owners, policies or decision lineage.")
    params = {"limit": limit + 1}
    audit = re.search(r"bafin\s+(?:audit\s*)?#?\s*(\d+)\b", text)
    entity = re.search(r"\b(?:svc|pipeline)_[a-z0-9_]+\b", text)
    # Treat identifiers as opaque, case-sensitive graph keys. Only the
    # surrounding natural-language question is normalized for intent matching.
    decision = re.search(r"\bdec-[a-z0-9]+(?:-[a-z0-9]+)*\b", question.strip(), re.IGNORECASE)
    if ("owns" in text or "owner" in text) and audit:
        intent = "audit_service_owners"
        params["audit_pattern"] = rf"[\s\S]*\baudit\s+#?{int(audit[1])}\b[\s\S]*"
        query = f"""
            MATCH (dec:Decision)-[governs:GOVERNS]->(p:DataPipeline)
            WHERE NOT (dec)<-[:SUPERSEDES]-()
            WITH dec, governs, p
            MATCH policy_path=(dec)-[:SUPERSEDES*0..]->(audit_dec:Decision)
            MATCH (audit_dec)-[audit_governs:GOVERNS]->(p)
            WHERE toLower(audit_dec.summary) =~ $audit_pattern
              AND (length(policy_path) = 0 OR
                   all(policy IN nodes(policy_path) WHERE policy.forbids_non_eu = true))
            MATCH path=(service:Service)-[:DEPENDS_ON*1..4]->(p)
            OPTIONAL MATCH (owner:Person)-[owns:OWNS]->(service)
            OPTIONAL MATCH (pipeline_owner:Person)-[pipeline_owns:OWNS]->(p)
            RETURN service.id, service.name, owner.name, owner.slack_handle,
                   pipeline_owner.name, pipeline_owner.slack_handle, dec.id,
                   audit_dec.id, length(policy_path) > 0,
                   {PATH_NODES} + {PATH_NODES.replace('path', 'policy_path')}
                     + {_optional(_node('owner'), 'owner')}
                     + {_optional(_node('pipeline_owner'), 'pipeline_owner')},
                   {PATH_EDGES} + {PATH_EDGES.replace('path', 'policy_path')}
                     + [{_edge('governs', 'dec', 'p')}, {_edge('audit_governs', 'audit_dec', 'p')}]
                     + {_optional(_edge('owns', 'owner', 'service'), 'owns')}
                     + {_optional(_edge('pipeline_owns', 'pipeline_owner', 'p'), 'pipeline_owns')}
            ORDER BY service.id, owner.name, pipeline_owner.name, dec.id, audit_dec.id LIMIT $limit
        """
        columns = ["service_id", "service_name", "service_owner", "service_owner_slack",
                   "pipeline_owner", "pipeline_owner_slack", "active_decision_id",
                   "audit_decision_id", "audit_context_inherited"]
        explanation = (
            "Current policies reference the audit directly or inherit its regional restriction "
            "through an uninterrupted SUPERSEDES chain with forbids_non_eu=true. "
            "Audit source decisions and lineage are included as evidence; waived restrictions "
            "are not treated as current. Dependent services are bounded to four hops. "
            "Service owners and pipeline owners are separate; missing ownership is not inferred."
        )
    elif "supersed" in text and not decision:
        intent = "superseded_decisions"
        date_filter = ""
        if "today" in text or "yesterday" in text:
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                raise ValueError("The reference time must include a timezone.")
            start = current.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            if "yesterday" in text:
                start -= timedelta(days=1)
            params.update(start_ts=int(start.timestamp()), end_ts=int((start + timedelta(days=1)).timestamp()))
            date_filter = "WHERE newer.timestamp >= $start_ts AND newer.timestamp < $end_ts"
        elif re.search(r"\b(last|this|since|on|week|month|year)\b|\d{4}-\d{2}-\d{2}", text):
            raise ValueError("For dated supersession questions, use today or yesterday (UTC).")
        query = f"""
            MATCH path=(newer:Decision)-[:SUPERSEDES]->(older:Decision)
            {date_filter}
            RETURN older.id, older.summary, newer.id, newer.summary, newer.timestamp,
                   {PATH_NODES}, {PATH_EDGES}
            ORDER BY newer.timestamp DESC, newer.id, older.id LIMIT $limit
        """
        columns = ["superseded_decision_id", "old_summary", "superseding_decision_id", "new_summary", "superseded_at_unix_utc"]
        explanation = "Historical SUPERSEDES edges, dated by the superseding decision's timestamp in UTC; historical decisions are intentionally included."
    elif "supersed" in text and decision:
        intent = "decision_lineage"
        params["decision_id"] = decision[0]
        requested = re.escape(decision[0].lower())
        subject = r"(?:what|which decisions?)"
        predecessors = re.fullmatch(
            rf"(?:{subject} (?:did|does|has) {requested} supersede(?:d)?|"
            rf"{subject} (?:was|were) superseded by {requested})[?.]?", text,
        )
        successors = re.fullmatch(
            rf"(?:{subject} (?:(?:has|have) )?superseded {requested}|"
            rf"{subject} supersedes {requested})[?.]?", text,
        )
        if not predecessors and not successors:
            raise ValueError(
                "Specify the lineage direction: 'What superseded DEC-2026-101?' or "
                "'Which decisions did DEC-2026-409 supersede?'"
            )
        if predecessors:
            pattern = "(newer:Decision {id: $decision_id})-[:SUPERSEDES*1..4]->(older:Decision)"
            detail = "older"
            explanation = "Predecessors superseded by the requested decision within four SUPERSEDES hops, including indirect predecessors."
        else:
            pattern = "(newer:Decision)-[:SUPERSEDES*1..4]->(older:Decision {id: $decision_id})"
            detail = "newer"
            explanation = "Successors of the requested decision within four SUPERSEDES hops, including intermediate historical successors."
        query = f"""
            MATCH path={pattern}
            RETURN older.id, newer.id, {detail}.summary, {detail}.timestamp,
                   {PATH_NODES}, {PATH_EDGES}
            ORDER BY {detail}.timestamp DESC, {detail}.id LIMIT $limit
        """
        columns = ["superseded_decision_id", "superseding_decision_id", "summary", "timestamp"]
    elif ("owns" in text or "owner" in text) and entity:
        intent = "entity_owners"
        params["entity_id"] = entity[0]
        query = f"""
            MATCH (entity {{id: $entity_id}})
            WHERE entity:Service OR entity:DataPipeline
            OPTIONAL MATCH (owner:Person)-[owns:OWNS]->(entity)
            RETURN entity.id, entity.name, owner.name, owner.slack_handle,
                   [{_node('entity')}] + {_optional(_node('owner'), 'owner')},
                   {_optional(_edge('owns', 'owner', 'entity'), 'owns')}
            ORDER BY owner.name LIMIT $limit
        """
        columns = ["entity_id", "entity_name", "owner_name", "owner_slack"]
        explanation = "Directly recorded ownership only; null owner fields mean no owner is recorded."
    elif re.search(r"\b(active|current|latest)\b", text) and re.search(r"\b(policies|policy|decisions|decision)\b", text):
        intent = "active_policies"
        clause = "AND p.id = $entity_id" if entity else ""
        if entity:
            params["entity_id"] = entity[0]
        query = f"""
            MATCH (dec:Decision)-[governs:GOVERNS]->(p)
            WHERE NOT (dec)<-[:SUPERSEDES]-() {clause}
            RETURN dec.id, dec.summary, dec.source_url, dec.forbids_non_eu, p.id,
                   [{_node('dec')}, {_node('p')}], [{_edge('governs', 'dec', 'p')}]
            ORDER BY dec.id, p.id LIMIT $limit
        """
        columns = ["decision_id", "summary", "source_url", "forbids_non_eu", "governed_entity_id"]
        explanation = "Current decisions are identified by the absence of incoming SUPERSEDES edges, retaining historical audit trails."
    else:
        raise ValueError("Supported questions include: " + " · ".join(EXAMPLES))
    return {"intent": intent, "cypher": query.strip(), "parameters": params,
            "columns": columns, "explanation": explanation, "limit": limit}


def query_company_brain(graph, question, *, limit=50, now=None):
    plan = plan_company_brain_question(question, limit=limit, now=now)
    result = graph.ro_query(plan["cypher"], plan["parameters"], timeout=2000)
    nodes, edges, rows = {}, {}, []
    for record in result.result_set[:limit]:
        rows.append(dict(zip(plan["columns"], record[:-2])))
        for node_id, labels, props in record[-2]:
            nodes[str(node_id)] = {"id": str(node_id), "labels": labels, "properties": props}
        for edge_id, source, target, kind, props in record[-1]:
            edges[str(edge_id)] = {"id": str(edge_id), "source": str(source), "target": str(target),
                                  "type": kind, "properties": props}
    return {
        "schema_uri": SCHEMA_URI, "question": question, "intent": plan["intent"],
        "cypher": plan["cypher"], "parameters": plan["parameters"],
        "explanation": plan["explanation"], "rows": rows,
        "truncated": len(result.result_set) > limit, "limit": limit,
        "queried_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subgraph": {"graph_id": f"company_brain_{uuid.uuid4().hex[:8]}",
                     "nodes": list(nodes.values()), "edges": list(edges.values())},
    }
