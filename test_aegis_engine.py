"""Unit tests plus opt-in live tests (AEGIS_TEST_FALKORDB=1)."""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aegis_falkordb_engine import AegisGraphEngine


def make_engine(nodes, edges=()):
    engine = AegisGraphEngine.__new__(AegisGraphEngine)
    rows = []
    for node in nodes:
        targets = [target for source, target in edges if source == node]
        rows.extend((node, f"Service {node}", target) for target in targets or [None])
    engine.graph = Mock()
    engine.graph.query.return_value = SimpleNamespace(result_set=rows)
    return engine


def by_id(engine):
    return {row["service_id"]: row for row in engine.compute_service_criticality()}


def test_dependency_hub_has_highest_blast_radius():
    engine = make_engine("abcd", [("b", "a"), ("c", "a"), ("d", "a")])
    ranked = engine.compute_service_criticality()
    assert ranked[0] == {
        "service_id": "a", "service_name": "Service a",
        "in_degree": 3, "out_degree": 0,
        "degree_centrality": 1.0, "betweenness_centrality": 0.0,
        "criticality_score": 0.5,
    }
    assert [row["service_id"] for row in ranked] == list("abcd")


def test_directed_chain_identifies_bridge():
    result = by_id(make_engine("abc", [("a", "b"), ("b", "c")]))
    assert result["b"]["degree_centrality"] == 0.5
    assert result["b"]["betweenness_centrality"] == 0.5
    assert result["b"]["criticality_score"] == 0.5
    assert result["a"]["degree_centrality"] == 0.0
    assert result["c"]["betweenness_centrality"] == 0.0


def test_equal_shortest_paths_split_betweenness():
    result = by_id(make_engine("abcd", [
        ("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"),
    ]))
    for node in "bc":
        assert result[node]["betweenness_centrality"] == pytest.approx(1 / 12)
    assert result["d"]["degree_centrality"] == pytest.approx(2 / 3)


def test_directed_cycle_terminates_and_has_symmetric_scores():
    ranked = make_engine("cba", [("a", "b"), ("b", "c"), ("c", "a")]).compute_service_criticality()
    assert [row["service_id"] for row in ranked] == list("abc")
    for row in ranked:
        assert row["degree_centrality"] == 0.5
        assert row["betweenness_centrality"] == 0.5


@pytest.mark.parametrize("nodes", ["", "a", "ba"])
def test_empty_and_isolated_services(nodes):
    ranked = make_engine(nodes).compute_service_criticality()
    assert [row["service_id"] for row in ranked] == sorted(nodes)
    for row in ranked:
        assert row["degree_centrality"] == 0.0
        assert row["betweenness_centrality"] == 0.0
        assert row["criticality_score"] == 0.0


def test_two_services_normalize_degree_without_dividing_betweenness_by_zero():
    result = by_id(make_engine("ab", [("a", "b")]))
    assert result["b"]["degree_centrality"] == 1.0
    assert result["b"]["betweenness_centrality"] == 0.0


def test_disconnected_service_is_included_in_normalization():
    result = by_id(make_engine("abcd", [("a", "b"), ("b", "c")]))
    assert result["b"]["degree_centrality"] == pytest.approx(1 / 3)
    assert result["b"]["betweenness_centrality"] == pytest.approx(1 / 6)
    assert result["d"]["criticality_score"] == 0.0


def test_duplicate_edges_and_self_dependencies_do_not_inflate_scores():
    simple = by_id(make_engine("abc", [("a", "b"), ("b", "c")]))
    noisy = by_id(make_engine("abc", [
        ("a", "b"), ("a", "b"), ("b", "c"), ("b", "b"), ("c", "c"),
    ]))
    assert noisy == simple


def test_reads_all_services_and_only_directed_service_dependencies():
    engine = make_engine("a")
    engine.compute_service_criticality()
    engine.graph.query.assert_called_once()
    query = " ".join(engine.graph.query.call_args.args[0].split())
    assert "MATCH (service:Service)" in query
    assert "OPTIONAL MATCH (service)-[:DEPENDS_ON]->(dependency:Service)" in query
    assert "RETURN service.id, service.name, dependency.id" in query
    engine.graph.delete.assert_not_called()


def test_database_errors_propagate():
    engine = make_engine("")
    engine.graph.query.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        engine.compute_service_criticality()


@pytest.fixture
def live_engine():
    """Use a unique disposable graph; never seed or delete production data."""
    if os.getenv("AEGIS_TEST_FALKORDB") != "1":
        pytest.skip("Set AEGIS_TEST_FALKORDB=1 to run live FalkorDB tests")
    engine = AegisGraphEngine(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", "6379")),
        password=os.getenv("FALKORDB_PASSWORD"),
    )
    engine.main_graph_name = f"aegis_test_{uuid.uuid4().hex}"
    engine.graph = engine.db.select_graph(engine.main_graph_name)
    engine.graph.query("RETURN 1")
    try:
        yield engine
    finally:
        engine.graph.delete()


def test_live_empty_graph(live_engine):
    assert live_engine.compute_service_criticality() == []


def test_live_topology_filters_non_services_and_unrelated_relationships(live_engine):
    live_engine.graph.query("""
        CREATE (a:Service {id: 'a', name: 'API'})
        CREATE (b:Service {id: 'b', name: 'Bridge'})
        CREATE (c:Service {id: 'c', name: 'Core'})
        CREATE (d:Service {id: 'd', name: 'Isolated'})
        CREATE (pipeline:DataPipeline {id: 'pipeline'})
        CREATE (owner:Person {id: 'owner'})
        CREATE (a)-[:DEPENDS_ON]->(b)-[:DEPENDS_ON]->(c)
        CREATE (a)-[:DEPENDS_ON]->(b)
        CREATE (b)-[:DEPENDS_ON]->(b)
        CREATE (c)-[:DEPENDS_ON]->(pipeline)-[:DEPENDS_ON]->(a)
        CREATE (d)-[:ROUTES_THROUGH]->(c)
        CREATE (owner)-[:OWNS]->(d)
    """)
    before_nodes = live_engine.graph.query("MATCH (n) RETURN count(n)").result_set
    before_edges = live_engine.graph.query("MATCH ()-[r]->() RETURN count(r)").result_set
    ranked = live_engine.compute_service_criticality()
    result = {row["service_id"]: row for row in ranked}
    assert set(result) == set("abcd")
    assert ranked[0]["service_id"] == "b"
    assert result["b"]["service_name"] == "Bridge"
    assert result["b"]["in_degree"] == result["b"]["out_degree"] == 1
    assert result["b"]["degree_centrality"] == pytest.approx(1 / 3)
    assert result["b"]["betweenness_centrality"] == pytest.approx(1 / 6)
    assert result["b"]["criticality_score"] == pytest.approx(0.25)
    assert result["d"]["criticality_score"] == 0.0
    assert live_engine.graph.query("MATCH (n) RETURN count(n)").result_set == before_nodes
    assert live_engine.graph.query("MATCH ()-[r]->() RETURN count(r)").result_set == before_edges


def test_live_seeded_universe(live_engine):
    live_engine.seed_tri_layer_universe()
    ranked = live_engine.compute_service_criticality()
    assert len(ranked) == 5
    assert ranked[0]["service_id"] == "svc_core_ledger"
    assert ranked[0]["degree_centrality"] == pytest.approx(0.25)
    assert ranked[0]["betweenness_centrality"] == 0.0
    assert ranked[0]["criticality_score"] == pytest.approx(0.125)


def test_live_shortest_path_fractions(live_engine):
    live_engine.graph.query("""
        CREATE (a:Service {id: 'a'}), (b:Service {id: 'b'}),
               (c:Service {id: 'c'}), (d:Service {id: 'd'})
        CREATE (a)-[:DEPENDS_ON]->(b)-[:DEPENDS_ON]->(d)
        CREATE (a)-[:DEPENDS_ON]->(c)-[:DEPENDS_ON]->(d)
    """)
    result = by_id(live_engine)
    for node in "bc":
        assert result[node]["betweenness_centrality"] == pytest.approx(1 / 12)
    assert result["d"]["degree_centrality"] == pytest.approx(2 / 3)


def test_live_compliance_report_matches_committed_handoff(live_engine):
    from aegis_compliance_report import build_compliance_report, fetch_handoff_provenance

    live_engine.seed_tri_layer_universe()
    pipeline = "pipeline_eu_payments"
    logs = []
    outcome = None
    provenance = []
    for candidate in live_engine.recall_playbooks_and_company_brain(pipeline):
        sim = live_engine.run_shadow_graph_simulation(pipeline, candidate["candidate_service_id"])
        logs.append({"candidate": candidate, "simulation": sim})
        if sim["simulation_approved"]:
            outcome = live_engine.coordinate_handoff_and_commit(
                pipeline, candidate["playbook_id"], sim,
                candidate["expert_handle"], candidate["policy_source"],
            )
            provenance = fetch_handoff_provenance(live_engine.graph, outcome, sim)
            break
    assert outcome is not None
    assert len(logs) == 2
    assert logs[0]["simulation"]["policy_violation"] is True
    assert len(provenance) == 1
    trace = provenance[0]
    assert trace["handoff_lock_id"] == outcome["handoff_lock_id"]
    assert trace["shadow_graph_verified"] == logs[-1]["simulation"]["shadow_graph_id"]
    assert trace["planner_id"] == "agent_planner"
    assert trace["auditor_id"] == "agent_auditor"
    assert trace["executor_id"] == "agent_executor"
    assert trace["episode_id"] == outcome["recorded_episode_id"]
    assert trace["playbook_id"] == logs[-1]["candidate"]["playbook_id"]
    assert trace["status"] == "APPROVED_BY_AUDITOR"
    assert fetch_handoff_provenance(live_engine.graph, outcome, {
        "shadow_graph_id": "shadow_sim_unrelated",
    }) == []
    markdown = build_compliance_report(
        logs, outcome, provenance, pipeline_id=pipeline,
        completed_at="2026-10-15T12:00:00+00:00",
    )
    assert "attempt 1" in markdown and "attempt 2" in markdown
    assert "Evidence incomplete" not in markdown
    assert "AUTHORIZED_EXECUTION" in markdown
    assert outcome["handoff_lock_id"] in markdown


def test_live_dashboard_export_and_reset(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    # Route the dashboard into the fixture's isolated graph, including resets.
    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        assert not app.exception
        assert app.get("download_button")[0].proto.disabled
        triage = next(button for button in app.button if "Run Shadow-Graph" in button.label)
        triage.click().run(timeout=30)
        assert not app.exception
        assert not app.get("download_button")[0].proto.disabled
        captured = app.session_state["compliance_report"]
        assert "BaFin Audit #88" in captured
        assert "Persisted graph trace" in captured
        assert "Evidence incomplete" not in captured
        assert app.session_state["last_outcome"]["handoff_lock_id"] in captured
        app.run(timeout=30)
        assert app.session_state["compliance_report"] == captured
        reset = next(button for button in app.button if "Reset & Seed" in button.label)
        reset.click().run(timeout=30)
        assert not app.exception
        assert app.session_state["compliance_report"] is None
        assert app.get("download_button")[0].proto.disabled
    finally:
        st.cache_resource.clear()


def test_live_benchmark_uses_lineage_and_respects_changed_policy(live_engine):
    from aegis_vector_rag_baseline import run_side_by_side_benchmark

    live_engine.seed_tri_layer_universe()
    result = run_side_by_side_benchmark(live_engine)
    assert result["vector_results"][0]["decision_id"] == "DEC-2026-101"
    assert {row["active_decision_id"] for row in result["graph_results"]} == {"DEC-2026-409"}
    assert {row["superseded_decision_id"] for row in result["graph_results"]} == {"DEC-2026-101"}
    assert result["simulation"]["governing_decision"] == "DEC-2026-409"
    assert result["simulation"]["policy_violation"] is True
    assert result["simulation"]["simulation_approved"] is False
    for key in ("vector_ms", "graph_ms", "simulation_ms"):
        assert result[key] >= 0
    live_engine.graph.query("""
        MATCH (old:Decision {id: 'DEC-2026-409'})-[:GOVERNS]->(p:DataPipeline)
        SET old.active = false
        CREATE (waiver:Decision {id: 'DEC-WAIVER', active: true, forbids_non_eu: false,
                               summary: 'US failover authorized'})
        CREATE (waiver)-[:SUPERSEDES]->(old)
        CREATE (waiver)-[:GOVERNS]->(p)
    """)
    changed = run_side_by_side_benchmark(live_engine)
    assert {row["active_decision_id"] for row in changed["graph_results"]} == {"DEC-WAIVER"}
    assert changed["simulation"]["simulation_approved"] is True
    assert changed["simulation"]["policy_violation"] is False
    assert live_engine.graph.query("MATCH (lock:HandoffLock) RETURN count(lock)").result_set == [[0]]
    assert live_engine.graph.query("""
        MATCH (:DataPipeline)-[:ROUTES_THROUGH {active: true}]->(svc:Service)
        RETURN svc.id
    """).result_set == [["svc_eu_gateway_primary"]]


def test_live_dashboard_benchmark_button(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        assert not app.exception
        next(button for button in app.button if button.label == "Run Side-by-Side Benchmark").click().run(timeout=30)
        assert not app.exception
        result = app.session_state["rag_benchmark"]
        assert result["vector_results"][0]["decision_id"] == "DEC-2026-101"
        assert any("US failover BLOCKED" in message.value for message in app.success)
        assert any("superseded" in message.value for message in app.error)
        assert {metric.label for metric in app.metric} >= {
            "Vector retrieval time", "Graph retrieval time", "US shadow verification time",
        }
        app.run(timeout=30)
        assert app.session_state["rag_benchmark"] == result
        next(button for button in app.button if "Lift EU Freeze" in button.label).click().run(timeout=30)
        assert app.session_state["rag_benchmark"] is None
        next(button for button in app.button if button.label == "Run Side-by-Side Benchmark").click().run(timeout=30)
        assert not app.exception
        assert any("US failover allowed" in message.value for message in app.warning)
        assert not any("US failover BLOCKED" in message.value for message in app.success)
    finally:
        st.cache_resource.clear()


@pytest.mark.parametrize("target,approved", [
    ("svc_us_failover_fast", False), ("svc_eu_zurich_dr", True),
])
def test_live_shadow_snapshot_survives_cleanup(live_engine, target, approved):
    import json

    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", target)
    assert report["simulation_approved"] is approved
    snapshot = json.loads(json.dumps(report["shadow_snapshot"]))
    assert snapshot["graph_id"] == report["shadow_graph_id"]
    assert len(snapshot["nodes"]) == 7
    assert len(snapshot["edges"]) == 6
    nodes = {node["id"]: node for node in snapshot["nodes"]}
    routes = [edge for edge in snapshot["edges"] if edge["type"] == "ROUTES_THROUGH"]
    assert len(routes) == 1
    route = routes[0]
    assert route["properties"]["simulated"] is True
    assert nodes[route["source"]]["properties"]["id"] == "pipeline_eu_payments"
    assert nodes[route["source"]]["properties"]["status"] == "SIMULATED_REROUTE"
    assert nodes[route["target"]]["properties"]["id"] == target
    assert all(edge["source"] in nodes and edge["target"] in nodes for edge in snapshot["edges"])
    assert report["shadow_graph_id"] not in live_engine.db.list_graphs()
    assert live_engine.graph.query("""
        MATCH (:DataPipeline)-[:ROUTES_THROUGH {active: true}]->(svc:Service)
        RETURN svc.id
    """).result_set == [["svc_eu_gateway_primary"]]


@pytest.mark.parametrize("failure_stage", ["build", "verify", "nodes", "edges"])
def test_shadow_graph_deleted_on_verification_or_export_failure(failure_stage):
    engine = AegisGraphEngine.__new__(AegisGraphEngine)
    engine.graph = Mock()
    pipeline = {"id": "pipeline", "throughput_tps": 4500}
    candidate = {"id": "candidate", "base_latency_ms": 70, "current_load_pct": 31,
                 "max_throughput_tps": 12000}
    checkout = {"id": "checkout", "base_latency_ms": 40, "current_load_pct": 65,
                "max_throughput_tps": 20000}
    paths = [[[checkout, pipeline], [{}], {"id": "sla", "max_allowed_latency_ms": 250}]]
    engine.graph.query.side_effect = [
        SimpleNamespace(result_set=[[
            pipeline, candidate, {"id": "eu", "gdpr_compliant": True},
            {"id": "decision", "forbids_non_eu": True}, False, None,
        ]]), SimpleNamespace(result_set=paths),
    ]
    engine.graph.ro_query.return_value.result_set = []
    engine._simulation_inputs = Mock(return_value={"nodes": [], "edges": []})
    shadow = Mock()
    engine.db = Mock()
    engine.db.select_graph.return_value = shadow
    def query(cypher, *args):
        stage = ("build" if "CREATE (p:DataPipeline)" in cypher else
                 "verify" if "MATCH path=" in cypher else
                 "nodes" if "labels(n)" in cypher else
                 "edges" if "id(edge)" in cypher else None)
        if stage == failure_stage:
            raise RuntimeError("stage failure")
        return SimpleNamespace(result_set=paths if stage == "verify" else [])
    shadow.query.side_effect = query
    with pytest.raises(RuntimeError, match="stage failure"):
        engine.run_shadow_graph_simulation("pipeline", "candidate")
    shadow.delete.assert_called_once()


def test_live_dashboard_shadow_diff(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    import streamlit_agraph
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    rendered = []
    monkeypatch.setattr(streamlit_agraph, "agraph", lambda **kwargs: rendered.append(kwargs))
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        app.toggle(key="inspect_shadow_diff").set_value(True).run(timeout=30)
        assert any("capture a sandbox snapshot" in message.value for message in app.info)
        next(button for button in app.button if "Run Shadow-Graph" in button.label).click().run(timeout=30)
        assert not app.exception
        choices = next(box for box in app.selectbox if box.label == "Shadow simulation attempt")
        assert len(choices.options) == 2
        for graph_id in choices.options:
            rendered.clear()
            next(box for box in app.selectbox if box.label == "Shadow simulation attempt").select(graph_id).run(timeout=30)
            assert not app.exception
            assert len(rendered) == 2
            yellow = [edge for edge in rendered[1]["edges"] if edge.color == "#facc15"]
            assert len(yellow) == 1
            assert yellow[0].label == "ROUTES_THROUGH (simulated)"
            assert yellow[0].width == 5
            assert yellow[0].source.startswith(graph_id + ":")
            assert graph_id not in live_engine.db.list_graphs()
        assert any(button.proto.label == "Download shadow snapshot JSON" for button in app.get("download_button"))
    finally:
        st.cache_resource.clear()


@pytest.mark.parametrize("scenario", ["ledger_lock", "throughput_spike", "sla_upgrade"])
def test_live_enterprise_scenarios_reject_unsafe_routes(live_engine, scenario):
    live_engine.seed_tri_layer_universe()
    live_engine.inject_chaos_scenario(scenario)
    # Repeated injection must not create duplicate replicas or contracts.
    before = live_engine.graph.query("MATCH (n) RETURN count(n)").result_set
    live_engine.inject_chaos_scenario(scenario)
    assert live_engine.graph.query("MATCH (n) RETURN count(n)").result_set == before
    for target in ["svc_us_failover_fast", "svc_eu_zurich_dr"]:
        report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", target)
        assert report["simulation_approved"] is False
        assert report["capacity_passed"] is (scenario == "sla_upgrade" and target == "svc_us_failover_fast")
        assert report["chaos_scenario"] == scenario
        assert report["rejection_reasons"]
        assert report["shadow_graph_id"] not in live_engine.db.list_graphs()
        with pytest.raises(ValueError, match="rejected"):
            live_engine.coordinate_handoff_and_commit(
                "pipeline_eu_payments", "pb_reroute_eu_sovereign", report, "@sarah", "policy",
            )
        checks = {check["service_id"]: check for check in report["capacity_checks"]}
        if scenario == "ledger_lock":
            assert checks["svc_core_ledger"]["lock_blocked"]
            assert checks["svc_us_ledger_replica"]["lock_blocked"]
            assert {check["region"] for check in report["capacity_checks"]} >= {"eu-central-1", "us-east-1"}
            assert any(path["service_path"] == [
                "svc_us_settlement_api", "svc_us_ledger_replica", "svc_core_ledger", "pipeline_eu_payments",
            ] for path in report["dependency_paths"])
            assert report["projected_e2e_latency_ms"] > 10000
            assert report["sla_passed"] is False
        elif scenario == "throughput_spike":
            assert report["required_throughput_tps"] == 18000
            if target == "svc_eu_zurich_dr":
                assert checks[target]["current_load_pct"] == 95
                assert checks[target]["projected_load_pct"] > 90
            assert report["sla_passed"] is True
        else:
            assert report["required_throughput_tps"] == 10000
            slas = {check["sla_id"]: check for check in report["sla_checks"]}
            assert slas["sla_enterprise_eu"]["sla_passed"] is True
            assert slas["sla_platinum_upgrade"]["sla_passed"] is False
            assert slas["sla_platinum_upgrade"]["sla_limit_ms"] == 100
            assert report["penalty_at_risk_usd"] == 90000
    assert live_engine.graph.query("MATCH (lock:HandoffLock) RETURN count(lock)").result_set == [[0]]


def test_live_healthy_multihop_chain_passes_and_preserves_dependencies(live_engine):
    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert report["simulation_approved"] is True
    assert report["capacity_passed"] is True
    assert report["projected_e2e_latency_ms"] == 140
    assert report["required_throughput_tps"] == 4500
    checks = {check["service_id"]: check for check in report["capacity_checks"]}
    assert checks["svc_eu_zurich_dr"]["projected_load_pct"] == pytest.approx(68.5)
    assert checks["svc_core_ledger"]["projected_load_pct"] == 50
    assert ["svc_merchant_checkout", "svc_core_ledger", "pipeline_eu_payments"] in [
        path["service_path"] for path in report["dependency_paths"]
    ]
    nodes = {node["id"]: node["properties"]["id"] for node in report["shadow_snapshot"]["nodes"]}
    assert ("svc_merchant_checkout", "svc_core_ledger") in {
        (nodes[edge["source"]], nodes[edge["target"]]) for edge in report["shadow_snapshot"]["edges"]
        if edge["type"] == "DEPENDS_ON"
    }


@pytest.mark.parametrize("load,passed", [(52.5, True), (52.5001, False)])
def test_live_capacity_threshold_includes_incoming_pipeline_load(live_engine, load, passed):
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query("MATCH (s:Service {id: 'svc_eu_zurich_dr'}) SET s.current_load_pct = $load", {"load": load})
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert report["capacity_passed"] is passed
    assert report["simulation_approved"] is passed


def test_live_downstream_bottleneck_rejects_otherwise_healthy_route(live_engine):
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query("MATCH (s:Service {id: 'svc_core_ledger'}) SET s.current_load_pct = 91")
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    checks = {check["service_id"]: check for check in report["capacity_checks"]}
    assert checks["svc_eu_zurich_dr"]["capacity_passed"] is True
    assert checks["svc_core_ledger"]["capacity_passed"] is False
    assert report["simulation_approved"] is False


def test_live_committed_capacity_is_retained_without_double_counting(live_engine):
    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    live_engine.coordinate_handoff_and_commit(
        "pipeline_eu_payments", "pb_reroute_eu_sovereign", report, "@sarah", "policy",
    )
    next_report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    target = next(check for check in next_report["capacity_checks"] if check["service_id"] == "svc_eu_zurich_dr")
    assert target["current_load_pct"] == pytest.approx(68.5)
    assert target["projected_load_pct"] == pytest.approx(68.5)
    assert next_report["simulation_approved"] is True


def test_live_unknown_chaos_does_not_mutate_graph(live_engine):
    with pytest.raises(ValueError, match="Unknown chaos"):
        live_engine.inject_chaos_scenario("unknown")


def test_live_dashboard_chaos_selector(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        assert not app.exception
        assert len(app.selectbox(key="chaos_scenario_selector").options) == 3
        for scenario in live_engine.CHAOS_SCENARIOS:
            live_engine.seed_tri_layer_universe()
            app.selectbox(key="chaos_scenario_selector").select(scenario).run(timeout=30)
            next(button for button in app.button if button.label == "Inject Chaos & Evaluate").click().run(timeout=30)
            assert not app.exception
            logs = app.session_state["simulation_logs"]
            assert len(logs) == 2
            assert all(not entry["simulation"]["simulation_approved"] for entry in logs)
            assert any(not entry["simulation"]["capacity_passed"] for entry in logs)
            assert app.session_state["last_outcome"] is None
            assert len(app.dataframe) == 4
            assert "Multi-hop capacity checks" in app.session_state["compliance_report"]
    finally:
        st.cache_resource.clear()


def test_live_company_brain_questions_return_evidence_subgraphs(live_engine):
    from aegis_company_brain import query_company_brain

    live_engine.seed_tri_layer_universe()
    before = live_engine.graph.query("MATCH (n) RETURN count(n)").result_set
    audit = query_company_brain(live_engine.graph, "Who owns services impacted by BaFin Audit #88?")
    assert {row["service_id"] for row in audit["rows"]} == {"svc_core_ledger", "svc_merchant_checkout"}
    ledger = next(row for row in audit["rows"] if row["service_id"] == "svc_core_ledger")
    assert ledger["service_owner"] is None
    assert ledger["pipeline_owner"] == "Sarah Chen"
    checkout = next(row for row in audit["rows"] if row["service_id"] == "svc_merchant_checkout")
    assert checkout["service_owner"] == "Sarah Chen"
    assert {edge["type"] for edge in audit["subgraph"]["edges"]} == {"DEPENDS_ON", "OWNS", "GOVERNS"}
    owner = query_company_brain(live_engine.graph, "Who owns svc_merchant_checkout?")
    assert owner["rows"][0]["owner_slack"] == "@sarah.chen"
    active = query_company_brain(live_engine.graph, "What are the active policies?")
    assert {row["decision_id"] for row in active["rows"]} == {"DEC-2026-409"}
    history = query_company_brain(live_engine.graph, "What superseded DEC-2026-101?")
    assert history["rows"][0]["superseding_decision_id"] == "DEC-2026-409"
    assert {edge["type"] for edge in history["subgraph"]["edges"]} == {"SUPERSEDES"}
    assert live_engine.graph.query("MATCH (n) RETURN count(n)").result_set == before


def test_live_company_brain_audit_number_is_exact(live_engine):
    from aegis_company_brain import query_company_brain

    live_engine.seed_tri_layer_universe()
    live_engine.graph.query("""
        MATCH (d:Decision {id: 'DEC-2026-409'}) SET d.summary = 'BaFin Audit #880'
    """)
    assert query_company_brain(live_engine.graph, "Who owns services impacted by BaFin Audit #88?")["rows"] == []
    assert len(query_company_brain(live_engine.graph, "Who owns services impacted by BaFin Audit #880?")["rows"]) == 2


def test_live_company_brain_today_boundaries(live_engine):
    from datetime import datetime, timezone
    from aegis_company_brain import query_company_brain

    live_engine.seed_tri_layer_universe()
    start = int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    for suffix, timestamp in [("before", start - 1), ("start", start), ("end", start + 86400)]:
        live_engine.graph.query("""
            CREATE (old:Decision {id: $old}), (new:Decision {id: $new, timestamp: $ts})
            CREATE (new)-[:SUPERSEDES]->(old)
        """, {"old": f"old_{suffix}", "new": f"new_{suffix}", "ts": timestamp})
    result = query_company_brain(
        live_engine.graph, "Which decisions were superseded today?",
        now=datetime(2026, 10, 15, 12, tzinfo=timezone.utc),
    )
    assert [row["superseded_decision_id"] for row in result["rows"]] == ["old_start"]
    assert len(result["subgraph"]["nodes"]) == 2
    assert len(result["subgraph"]["edges"]) == 1


def test_live_company_brain_mcp_tool(live_engine, monkeypatch, capsys):
    import asyncio
    import json
    import aegis_mcp_server as server

    live_engine.seed_tri_layer_universe()
    monkeypatch.setattr(server, "engine", live_engine)
    capsys.readouterr()
    result = server.query_company_brain_cypher("What are the active policies?")
    assert result["rows"][0]["decision_id"] == "DEC-2026-409"
    assert json.loads(server.get_tri_layer_schema())["query_contract"]["schema_uri"] == result["schema_uri"]
    assert "query_company_brain_cypher" in {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    json.dumps(result)
    assert capsys.readouterr().out == ""


def test_live_company_brain_chat_panel(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    import streamlit_agraph
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    rendered = []
    monkeypatch.setattr(streamlit_agraph, "agraph", lambda **kwargs: rendered.append(kwargs))
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        app.chat_input(key="company_brain_question").set_value("Who owns services impacted by BaFin Audit #88?").run(timeout=30)
        assert not app.exception
        assert len(app.chat_message) == 2
        answer = app.session_state["company_brain_history"][0]["answer"]
        assert len(answer["rows"]) == 2
        assert any("=~ $audit_pattern" in code.value for code in app.code)
        assert len(rendered[-1]["nodes"]) == len(answer["subgraph"]["nodes"])
        app.chat_input(key="company_brain_question").set_value("DELETE all nodes").run(timeout=30)
        assert not app.exception
        assert "error" in app.session_state["company_brain_history"][-1]
        assert any("read-only" in message.value for message in app.info)
        app.run(timeout=30)
        assert len(app.session_state["company_brain_history"]) == 2
    finally:
        st.cache_resource.clear()


def test_live_slack_thread_lineage_authorship_retry_and_simulation(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    freeze = ingest_slack_message(
        live_engine, "Urgent: Freeze all traffic to Zurich DR node due to maintenance",
        "@marcus.compliance", event_id="evt-freeze",
    )
    assert freeze["target_id"] == "svc_eu_zurich_dr"
    assert freeze["superseded_decision_ids"] == []
    authors = live_engine.graph.query("""
        MATCH (p:Person)-[:AUTHORED]->(d:Decision {id: $id})-[:GOVERNS]->(target)
        RETURN p.slack_handle, target.id, d.active, d.traffic_blocked, d.summary
    """, {"id": freeze["decision_id"]}).result_set
    assert authors[0][:4] == ["@marcus.compliance", "svc_eu_zurich_dr", True, True]
    assert authors[0][4] == "Urgent: Freeze all traffic to Zurich DR node due to maintenance"
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert report["capacity_passed"] and report["sla_passed"]
    assert report["traffic_freeze_violation"] is True
    assert report["simulation_approved"] is False
    assert report["governing_decision"] == freeze["decision_id"]
    assert freeze["decision_id"] in report["governing_decisions"]
    resume = ingest_slack_message(live_engine, "Resume traffic to Zurich", "@sarah.chen", event_id="evt-resume")
    assert resume["superseded_decision_ids"] == [freeze["decision_id"]]
    assert live_engine.graph.query("""
        MATCH (new:Decision {id: $new})-[:SUPERSEDES]->(old:Decision {id: $old})
        RETURN new.active, old.active
    """, {"new": resume["decision_id"], "old": freeze["decision_id"]}).result_set == [[True, False]]
    before = live_engine.graph.query("MATCH (d:Decision) RETURN count(d)").result_set
    duplicate = ingest_slack_message(
        live_engine, "Urgent: Freeze all traffic to Zurich DR node due to maintenance",
        "@marcus.compliance", event_id="evt-freeze",
    )
    assert duplicate["status"] == "DUPLICATE_EVENT"
    assert live_engine.graph.query("MATCH (d:Decision) RETURN count(d)").result_set == before
    with pytest.raises(ValueError, match="reused"):
        ingest_slack_message(live_engine, "Freeze ledger", "@sarah.chen", event_id="evt-freeze")
    assert live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")["simulation_approved"] is True
    assert live_engine.graph.query("""
        MATCH (d:Decision {id: 'DEC-2026-409'}) RETURN d.active
    """).result_set == [[True]]


def test_live_pipeline_slack_directive_inherits_regional_freeze(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    freeze = ingest_slack_message(live_engine, "Stop traffic to pipeline_eu_payments", "@sarah.chen")
    assert freeze["superseded_decision_ids"] == ["DEC-2026-409"]
    assert live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")["traffic_freeze_violation"]
    resume = ingest_slack_message(live_engine, "Resume pipeline_eu_payments", "@marcus.compliance")
    assert resume["superseded_decision_ids"] == [freeze["decision_id"]]
    rows = live_engine.graph.query("MATCH (d:Decision {id: $id}) RETURN d.forbids_non_eu", {"id": resume["decision_id"]}).result_set
    assert rows == [[True]]
    assert live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_us_failover_fast")["policy_violation"]
    assert live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")["simulation_approved"]


def test_live_slack_freeze_on_dependency_blocks_reroute(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    directive = ingest_slack_message(live_engine, "Block core ledger for maintenance", "@sarah.chen")
    report = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert report["simulation_approved"] is False
    assert report["traffic_freeze_violation"] is True
    evidence = next(check for check in report["policy_checks"] if check["decision_id"] == directive["decision_id"])
    assert evidence["target_id"] == "svc_core_ledger"
    assert evidence["author_handle"] == "@sarah.chen"
    snapshot = report["shadow_snapshot"]
    ids = {node["id"]: node["properties"]["id"] for node in snapshot["nodes"]}
    assert (directive["decision_id"], "svc_core_ledger") in {
        (ids[edge["source"]], ids[edge["target"]]) for edge in snapshot["edges"]
        if edge["type"] == "GOVERNS"
    }


def test_live_webhook_http_ingestion(live_engine):
    import hashlib
    import hmac
    from http.server import HTTPServer
    import json
    from threading import Thread
    import time
    from urllib.request import Request, urlopen
    from aegis_webhook_listener import make_handler

    live_engine.seed_tri_layer_universe()
    server = HTTPServer(("127.0.0.1", 0), make_handler(
        live_engine, signing_secret="test-secret", user_map={"U-MARCUS": "@marcus.compliance"},
    ))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {"type": "event_callback", "event_id": "Ev-signed-test", "event": {
            "type": "message", "user": "U-MARCUS", "text": "Freeze Zurich for maintenance",
            "channel": "C-COMPLIANCE", "ts": "1760000000.123",
        }}
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"test-secret", b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()
        request = Request(f"http://127.0.0.1:{server.server_port}/slack/events", data=body, headers={
            "Content-Type": "application/json", "X-Slack-Request-Timestamp": timestamp,
            "X-Slack-Signature": signature,
        })
        with urlopen(request, timeout=5) as response:
            result = json.load(response)
        assert result["status"] == "DECISION_RECORDED"
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["status"] == "DUPLICATE_EVENT"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_live_dashboard_incoming_slack_thread(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        app.text_area(key="slack_thread_text").set_value("Urgent: Freeze all traffic to Zurich DR node due to maintenance")
        next(button for button in app.button if button.label == "Simulate Incoming Slack Thread").click().run(timeout=30)
        assert not app.exception
        record = app.session_state["last_slack_ingestion"]
        assert record["author"] == "@marcus.compliance"
        assert record["target_id"] == "svc_eu_zurich_dr"
        next(button for button in app.button if "Run Shadow-Graph" in button.label).click().run(timeout=30)
        assert not app.exception
        assert app.session_state["last_outcome"] is None
        assert any(item["simulation"].get("traffic_freeze_violation") for item in app.session_state["simulation_logs"])
        app.selectbox(key="slack_author").select("@sarah.chen")
        app.text_area(key="slack_thread_text").set_value("Resume Zurich")
        next(button for button in app.button if button.label == "Simulate Incoming Slack Thread").click().run(timeout=30)
        assert not app.exception
        assert app.session_state["last_slack_ingestion"]["superseded_decision_ids"] == [record["decision_id"]]
    finally:
        st.cache_resource.clear()


def test_live_rejected_episode_decay_is_durable_and_idempotent(live_engine):
    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation(
        "pipeline_eu_payments", "svc_us_failover_fast",
        playbook_id="pb_reroute_us_east", incident_id="incident-1",
    )
    memory = report["rejection_memory"][0]
    assert memory["previous_weight"] == 0.94
    assert memory["weight"] == pytest.approx(0.94 * 0.8)
    assert live_engine.graph.query("""
        MATCH (auditor:Agent)-[:RECORDED]->(ep:RejectedEpisode)-[:USED_PLAYBOOK]->(pb:Playbook)
        MATCH (ep)-[:EVALUATED_UNDER]->(decision:Decision)
        RETURN auditor.id, ep.incident_id, ep.shadow_graph_id, pb.id, decision.id, ep.rejection_reasons
    """).result_set == [[
        "agent_auditor", "incident-1", report["shadow_graph_id"], "pb_reroute_us_east",
        "DEC-2026-409", report["rejection_reasons"],
    ]]
    duplicate = live_engine.record_rejected_episode("pipeline_eu_payments", "pb_reroute_us_east", report)
    assert duplicate == memory
    assert live_engine.graph.query("MATCH (ep:RejectedEpisode) RETURN count(ep)").result_set == [[1]]
    next_report = live_engine.run_shadow_graph_simulation(
        "pipeline_eu_payments", "svc_us_failover_fast", incident_id="incident-2",
    )
    assert next_report["rejection_memory"][0]["weight"] == pytest.approx(0.94 * 0.8**2)
    candidates = live_engine.recall_playbooks_and_company_brain("pipeline_eu_payments")
    assert candidates[0]["playbook_id"] == "pb_reroute_eu_sovereign"
    assert candidates[1]["historical_success_rate"] == pytest.approx(0.94 * 0.8**2)
    history = live_engine.get_playbook_weight_history()
    assert [entry["sequence"] for entry in history] == [0, 0, 1, 2]
    assert [entry["incident_id"] for entry in history[2:]] == ["incident-1", "incident-2"]
    assert report["shadow_graph_id"] not in live_engine.db.list_graphs()
    assert live_engine.graph.query("MATCH (lock:HandoffLock) RETURN count(lock)").result_set == [[0]]


def test_live_policy_changes_do_not_inherit_previous_penalties(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    old = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_us_failover_fast")
    old_context = old["rejection_memory"][0]["policy_context"]
    live_engine.graph.query("""
        MATCH (old:Decision {id: 'DEC-2026-409'})-[:GOVERNS]->(p:DataPipeline)
        SET old.active = false
        CREATE (waiver:Decision {id: 'DEC-WAIVER', active: true, forbids_non_eu: false,
                               summary: 'US routing approved'})
        CREATE (waiver)-[:SUPERSEDES]->(old)
        CREATE (waiver)-[:GOVERNS]->(p)
    """)
    candidates = live_engine.recall_playbooks_and_company_brain("pipeline_eu_payments")
    assert candidates[0]["playbook_id"] == "pb_reroute_us_east"
    assert candidates[0]["historical_success_rate"] == 0.94
    assert candidates[0]["policy_context"] != old_context
    passed = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_us_failover_fast")
    assert passed["simulation_approved"] is True
    assert passed["rejection_memory"] == []
    outcome = live_engine.coordinate_handoff_and_commit(
        "pipeline_eu_payments", "pb_reroute_us_east", passed, "@sarah", "waiver",
    )
    assert outcome["updated_playbook_success_rate"] == pytest.approx(round((0.94 * 18 + 1) / 19, 4))
    assert live_engine.graph.query("""
        MATCH (c:PlaybookContext {policy_context: $context}) RETURN c.success_rate
    """, {"context": old_context}).result_set == [[pytest.approx(0.752)]]
    # Candidate-specific Slack rules must participate in both recall and learning.
    ingest_slack_message(live_engine, "Freeze Zurich", "@marcus.compliance")
    frozen = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    frozen_context = frozen["rejection_memory"][0]["policy_context"]
    recalled = {row["playbook_id"]: row for row in live_engine.recall_playbooks_and_company_brain("pipeline_eu_payments")}
    assert recalled["pb_reroute_eu_sovereign"]["policy_context"] == frozen_context
    assert recalled["pb_reroute_eu_sovereign"]["historical_success_rate"] == pytest.approx(0.89 * 0.8)


def test_live_success_rewards_same_context_after_rejection(live_engine):
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query("MATCH (s:Service {id: 'svc_eu_zurich_dr'}) SET s.current_load_pct = 95")
    failed = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert failed["rejection_memory"][0]["weight"] == pytest.approx(0.712)
    live_engine.graph.query("MATCH (s:Service {id: 'svc_eu_zurich_dr'}) SET s.current_load_pct = 31")
    passed = live_engine.run_shadow_graph_simulation("pipeline_eu_payments", "svc_eu_zurich_dr")
    assert passed["simulation_approved"]
    # Passing a simulation alone must not earn the success reward.
    prior = live_engine.get_playbook_weight_history()
    assert prior[-1]["outcome"] == "REJECTED"
    live_engine.coordinate_handoff_and_commit("pipeline_eu_payments", "pb_reroute_eu_sovereign", passed, "@sarah", "policy")
    history = live_engine.get_playbook_weight_history()
    assert history[-1]["weight"] == pytest.approx((0.712 * 10 + 1) / 11)
    assert history[-1]["policy_context"] == history[-2]["policy_context"]
    assert history[-1]["outcome"] == "SUCCESS"
    with pytest.raises(ValueError, match="Only rejected"):
        live_engine.record_rejected_episode("pipeline_eu_payments", "pb_reroute_eu_sovereign", passed)


def test_live_dashboard_weight_charts_across_incidents(live_engine, monkeypatch):
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine

    monkeypatch.setattr(aegis_falkordb_engine, "AegisGraphEngine", lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file(str(Path(__file__).with_name("aegis_dashboard.py")))
        app.run(timeout=30)
        assert not app.exception
        assert len(app.get("vega_lite_chart")) + len(app.get("arrow_vega_lite_chart")) == 2
        app.selectbox(key="chaos_scenario_selector").select("throughput_spike").run(timeout=30)
        next(button for button in app.button if button.label == "Inject Chaos & Evaluate").click().run(timeout=30)
        assert not app.exception
        first_ids = {entry["simulation"]["incident_id"] for entry in app.session_state["simulation_logs"]}
        assert len(first_ids) == 1
        next(button for button in app.button if "Run Shadow-Graph" in button.label).click().run(timeout=30)
        assert not app.exception
        history = live_engine.get_playbook_weight_history()
        assert len(history) == 6
        assert len({row["incident_id"] for row in history[2:]}) == 2
        assert len(app.get("vega_lite_chart")) + len(app.get("arrow_vega_lite_chart")) == 2
        candidates = {row["playbook_id"]: row for row in live_engine.recall_playbooks_and_company_brain("pipeline_eu_payments")}
        assert candidates["pb_reroute_us_east"]["historical_success_rate"] == pytest.approx(0.94 * 0.8**2)
        assert candidates["pb_reroute_eu_sovereign"]["historical_success_rate"] == pytest.approx(0.89 * 0.8**2)
        app.run(timeout=30)
        assert len(live_engine.get_playbook_weight_history()) == len(history)
    finally:
        st.cache_resource.clear()


def test_live_commit_rejects_policy_changed_after_simulation(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')
    ingest_slack_message(live_engine, 'Freeze Zurich', '@marcus.compliance')
    before = live_engine.get_playbook_weight_history()
    with pytest.raises(ValueError, match='policy context changed'):
        live_engine.coordinate_handoff_and_commit(
            'pipeline_eu_payments', 'pb_reroute_eu_sovereign', report, '@sarah', 'policy')
    assert live_engine.get_playbook_weight_history() == before
    assert live_engine.graph.ro_query('MATCH (h:HandoffLock) RETURN count(h)').result_set == [[0]]
    assert live_engine.graph.ro_query('''
        MATCH (:DataPipeline {id: 'pipeline_eu_payments'})-[:ROUTES_THROUGH {active: true}]->(s)
        RETURN s.id
    ''').result_set != [['svc_eu_zurich_dr']]


def test_live_commit_retry_returns_original_receipt_without_reward(live_engine):
    from aegis_webhook_listener import ingest_slack_message

    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')
    args = ('pipeline_eu_payments', 'pb_reroute_eu_sovereign', report, '@sarah', 'policy')
    first = live_engine.coordinate_handoff_and_commit(*args)
    before = live_engine.get_playbook_weight_history()
    assert live_engine.coordinate_handoff_and_commit(*args) == first
    ingest_slack_message(live_engine, 'Freeze Zurich', '@marcus.compliance')
    assert live_engine.coordinate_handoff_and_commit(*args) == first
    assert live_engine.get_playbook_weight_history() == before
    assert live_engine.graph.ro_query('MATCH (h:HandoffLock) RETURN count(h)').result_set == [[1]]
    assert live_engine.graph.ro_query(
        'MATCH (e:Episode {incident_id: $incident}) RETURN count(e)',
        {'incident': report['incident_id']}).result_set == [[1]]


def test_live_recall_deduplicates_owners_before_penalizing(live_engine):
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query('''
        MATCH (p:DataPipeline {id: 'pipeline_eu_payments'}),
              (owner:Person {slack_handle: '@marcus.compliance'})
        CREATE (owner)-[:OWNS {expertise_score: 0.8}]->(p)
    ''')
    candidates = live_engine.recall_playbooks_and_company_brain('pipeline_eu_payments')
    assert len(candidates) == len({c['playbook_id'] for c in candidates}) == 2
    assert all(c['expert_handle'] == '@sarah.chen' for c in candidates)
    for candidate in candidates:
        report = live_engine.run_shadow_graph_simulation(
            'pipeline_eu_payments', candidate['candidate_service_id'],
            playbook_id=candidate['playbook_id'], incident_id='single-incident')
        if report['simulation_approved']:
            break
    events = [e for e in live_engine.get_playbook_weight_history() if e['outcome'] == 'REJECTED']
    assert len(events) == 1
    assert events[0]['weight'] == pytest.approx(0.752)


@pytest.mark.parametrize('mutation', [
    "MATCH (p:DataPipeline) SET p.throughput_tps = 18000",
    "MATCH (s:Service {id:'svc_eu_zurich_dr'}) SET s.current_load_pct = 95",
    "MATCH (s:Service {id:'svc_core_ledger'}) SET s.status = 'LOCKED'",
    "MATCH (s:SLA) SET s.max_allowed_latency_ms = 1",
    "MATCH (s:Service {id:'svc_merchant_checkout'}) CREATE (s)-[:BOUND_BY]->(:SLA {id:'new', max_allowed_latency_ms:1})",
    "MATCH ()-[r:DEPENDS_ON]->() DELETE r",
])
def test_live_commit_checks_all_simulation_inputs(live_engine, mutation):
    live_engine.seed_tri_layer_universe()
    report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')
    live_engine.graph.query(mutation)
    before = live_engine._simulation_inputs()
    with pytest.raises(ValueError, match='simulation inputs changed'):
        live_engine.coordinate_handoff_and_commit(
            'pipeline_eu_payments', 'pb_reroute_eu_sovereign', report, '@sarah', 'policy')
    assert live_engine._simulation_inputs() == before
    assert live_engine.graph.ro_query('MATCH (h:HandoffLock) RETURN count(h)').result_set == [[0]]


def test_live_regional_update_preserves_freeze_and_merges_predecessors(live_engine, monkeypatch):
    import aegis_mcp_server
    from aegis_webhook_listener import ingest_slack_message
    live_engine.seed_tri_layer_universe()
    monkeypatch.setattr(aegis_mcp_server, 'engine', live_engine)
    freeze = ingest_slack_message(live_engine, 'Freeze payments pipeline', '@marcus.compliance')
    live_engine.graph.query('''
        MATCH (p:DataPipeline) CREATE (:Decision {id:'SECOND', active:true})-[:GOVERNS]->(p)
    ''')
    result = aegis_mcp_server.inject_live_company_decision(
        'pipeline_eu_payments', 'REGIONAL', 'Enforce EU regions', True)
    assert result['traffic_blocked'] is True
    assert set(result['superseded_decision_ids']) == {freeze['decision_id'], 'SECOND'}
    assert live_engine.graph.ro_query("MATCH (d:Decision {id:'REGIONAL'}) RETURN count(d)").result_set == [[1]]
    assert not live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')['simulation_approved']
    with pytest.raises(ValueError, match='already exists'):
        aegis_mcp_server.inject_live_company_decision('pipeline_eu_payments', 'REGIONAL', 'Retry', False)
    ingest_slack_message(live_engine, 'Resume payments pipeline', '@marcus.compliance')
    assert live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')['simulation_approved']


def test_live_round_trip_routes_release_load(live_engine):
    live_engine.seed_tri_layer_universe()
    live_engine.inject_regional_decision('pipeline_eu_payments', 'WAIVER', 'Allow US', False)
    for target, playbook, expected in [
        ('svc_eu_zurich_dr', 'pb_reroute_eu_sovereign', 68.5),
        ('svc_us_failover_fast', 'pb_reroute_us_east', 44.5),
        ('svc_eu_zurich_dr', 'pb_reroute_eu_sovereign', 68.5),
        ('svc_eu_zurich_dr', 'pb_reroute_eu_sovereign', 68.5),
    ]:
        report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', target)
        assert report['simulation_approved']
        live_engine.coordinate_handoff_and_commit('pipeline_eu_payments', playbook, report, '@sarah', 'policy')
        assert live_engine.graph.ro_query('MATCH (s:Service {id:$id}) RETURN s.current_load_pct', {'id':target}).result_set == [[expected]]
    assert live_engine.graph.ro_query("MATCH (s:Service {id:'svc_us_failover_fast'}) RETURN s.current_load_pct").result_set == [[22.0]]


def test_live_dashboard_connection_error_does_not_reset(live_engine, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query("CREATE (:ReviewSentinel {id:'preserve'})")
    original = live_engine.graph.query
    def fail_initial_read(query, *args, **kwargs):
        if query == 'MATCH (n) RETURN count(n)':
            raise ConnectionError('transient outage')
        return original(query, *args, **kwargs)
    monkeypatch.setattr(live_engine.graph, 'query', fail_initial_read)
    monkeypatch.setattr(aegis_falkordb_engine, 'AegisGraphEngine', lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file('aegis_dashboard.py').run(timeout=30)
        assert not app.exception
        assert 'database connection' in app.error[0].value
        assert live_engine.graph.ro_query('MATCH (n:ReviewSentinel) RETURN count(n)').result_set == [[1]]
    finally:
        st.cache_resource.clear()


def test_live_legacy_active_route_load_is_released(live_engine):
    live_engine.seed_tri_layer_universe()
    live_engine.graph.query('MATCH ()-[r:ROUTES_THROUGH]->() REMOVE r.load_contribution_pct')
    report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')
    live_engine.coordinate_handoff_and_commit(
        'pipeline_eu_payments', 'pb_reroute_eu_sovereign', report, '@sarah', 'policy')
    assert live_engine.graph.ro_query("MATCH (s:Service {id:'svc_eu_gateway_primary'}) RETURN s.current_load_pct").result_set == [[56.5]]


def test_live_dashboard_regional_controls_preserve_pipeline_freeze(live_engine, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    import aegis_falkordb_engine
    from aegis_webhook_listener import ingest_slack_message
    live_engine.seed_tri_layer_universe()
    ingest_slack_message(live_engine, 'Freeze payments pipeline', '@marcus.compliance')
    monkeypatch.setattr(aegis_falkordb_engine, 'AegisGraphEngine', lambda **kwargs: live_engine)
    st.cache_resource.clear()
    try:
        app = AppTest.from_file('aegis_dashboard.py').run(timeout=30)
        for label in ['Enforce EU GDPR Freeze', 'Lift EU Freeze']:
            next(b for b in app.button if label in b.label).click().run(timeout=30)
            assert not app.exception
            report = live_engine.run_shadow_graph_simulation('pipeline_eu_payments', 'svc_eu_zurich_dr')
            assert report['traffic_freeze_violation']
            assert not report['simulation_approved']
    finally:
        st.cache_resource.clear()


def test_live_audit_ownership_survives_inherited_policy_updates(live_engine):
    from aegis_company_brain import query_company_brain
    from aegis_webhook_listener import ingest_slack_message
    live_engine.seed_tri_layer_universe()
    question = 'Who owns services impacted by BaFin Audit #88?'
    baseline = query_company_brain(live_engine.graph, question)
    for index in range(6):
        action = 'Freeze' if index % 2 == 0 else 'Resume'
        latest = ingest_slack_message(live_engine, f'{action} payments pipeline', '@marcus.compliance')
    result = query_company_brain(live_engine.graph, question)
    assert {r['service_id'] for r in result['rows']} == {r['service_id'] for r in baseline['rows']}
    assert all(r['active_decision_id'] == latest['decision_id'] for r in result['rows'])
    assert all(r['audit_decision_id'] == 'DEC-2026-409' for r in result['rows'])
    assert all(r['audit_context_inherited'] for r in result['rows'])
    assert sum(e['type'] == 'SUPERSEDES' for e in result['subgraph']['edges']) == 6
    ledger = next(r for r in result['rows'] if r['service_id'] == 'svc_core_ledger')
    assert ledger['service_owner'] is None
    assert ledger['pipeline_owner'] == 'Sarah Chen'
    live_engine.inject_regional_decision('pipeline_eu_payments', 'DEC-WAIVER', 'Allow US', False)
    assert query_company_brain(live_engine.graph, question)['rows'] == []
    # Reinstating a generic regional policy must not silently reactivate an old audit.
    live_engine.inject_regional_decision('pipeline_eu_payments', 'DEC-NEW-FREEZE', 'Enforce EU', True)
    assert query_company_brain(live_engine.graph, question)['rows'] == []


@pytest.mark.parametrize('question', [
    'Which decisions did DEC-2026-409 supersede?',
    'What did DEC-2026-409 supersede?',
    'Which decisions were superseded by DEC-2026-409?',
])
def test_live_predecessor_questions_follow_outgoing_supersedes(live_engine, question):
    from aegis_company_brain import query_company_brain
    from aegis_webhook_listener import ingest_slack_message
    live_engine.seed_tri_layer_universe()
    successor = ingest_slack_message(live_engine, 'Freeze payments pipeline', '@marcus.compliance')
    result = query_company_brain(live_engine.graph, question)
    assert len(result['rows']) == 1
    row = result['rows'][0]
    assert row['superseded_decision_id'] == 'DEC-2026-101'
    assert row['superseding_decision_id'] == 'DEC-2026-409'
    assert 'cross-Atlantic' in row['summary']
    assert successor['decision_id'] not in {n['properties']['id'] for n in result['subgraph']['nodes']}
    forward = query_company_brain(live_engine.graph, 'What superseded DEC-2026-409?')
    assert {r['superseding_decision_id'] for r in forward['rows']} == {successor['decision_id']}
    assert all(r['superseded_decision_id'] == 'DEC-2026-409' for r in forward['rows'])


def test_live_lineage_resolves_dashboard_ids_without_case_collisions(live_engine):
    from aegis_company_brain import query_company_brain
    live_engine.seed_tri_layer_universe()
    dashboard_id = f'DEC-EU-a{uuid.uuid4().hex[1:]}'
    uppercase_id = dashboard_id.upper()
    live_engine.inject_regional_decision('pipeline_eu_payments', dashboard_id, 'Keep EU routing', True)
    live_engine.inject_regional_decision('pipeline_eu_payments', uppercase_id, 'New EU policy', True)
    predecessors = query_company_brain(
        live_engine.graph, f'Which decisions did {dashboard_id} supersede?')
    assert predecessors['rows']
    assert {r['superseding_decision_id'] for r in predecessors['rows']} == {dashboard_id}
    assert {r['superseded_decision_id'] for r in predecessors['rows']} == {'DEC-2026-409', 'DEC-2026-101'}
    successors = query_company_brain(live_engine.graph, f'What superseded {dashboard_id}?')
    assert len(successors['rows']) == 1
    assert successors['rows'][0]['superseding_decision_id'] == uppercase_id
    assert successors['rows'][0]['superseded_decision_id'] == dashboard_id
    assert query_company_brain(live_engine.graph, f'What superseded {uppercase_id}?')['rows'] == []
