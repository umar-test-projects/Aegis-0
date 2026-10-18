// Select the seeded aegis_demo_verified graph. Execute each query separately.
// Mutating shadow/commit queries remain in source alongside their validation.

// Playbook, current policy and human owner recall
// Exact query from aegis_falkordb_engine.py::recall_playbooks_and_company_brain
// Parameters: pipeline_id = 'pipeline_eu_payments'
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
ORDER BY pb.success_rate DESC, owns.expertise_score DESC;

// Dependency paths and attached SLA constraints
// Exact query from aegis_falkordb_engine.py::run_shadow_graph_simulation
// Parameters: pipeline_id = 'pipeline_eu_payments'
MATCH path=(downstream:Service)-[:DEPENDS_ON*1..4]->
           (p:DataPipeline {id: $pipeline_id})
OPTIONAL MATCH (downstream)-[:BOUND_BY]->(sla:SLA)
RETURN [n IN nodes(path) | properties(n)],
       [r IN relationships(path) | properties(r)], properties(sla);

// Directed service dependency input to Brandes criticality
// Exact query from aegis_falkordb_engine.py::compute_service_criticality
// Parameters: none
MATCH (service:Service)
OPTIONAL MATCH (service)-[:DEPENDS_ON]->(dependency:Service)
RETURN service.id, service.name, dependency.id;
