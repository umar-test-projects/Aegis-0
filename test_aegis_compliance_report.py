from unittest.mock import Mock

from aegis_compliance_report import build_compliance_report, fetch_handoff_provenance


def sample_log(approved=False):
    return {
        "candidate": {
            "playbook_id": "pb_eu", "active_decision_id": "DEC-2026-409",
            "superseded_decision_id": "DEC-2026-101",
            "active_policy_summary": "EU only | no US\n<script>unsafe</script>",
            "policy_source": "https://example.test/policy", "forbids_non_eu": True,
            "expert_handle": "@sarah.chen",
        },
        "simulation": {
            "shadow_graph_id": "shadow_sim_123", "target_service": "svc_eu",
            "region": "eu-central-1", "projected_e2e_latency_ms": 140,
            "sla_limit_ms": 250, "sla_passed": True,
            "policy_violation": not approved, "simulation_approved": approved,
            "penalty_at_risk_usd": 15000, "governing_decision": "DEC-2026-409",
        },
    }


def report(logs, outcome=None, provenance=(), **kwargs):
    return build_compliance_report(
        logs, outcome, provenance, pipeline_id="pipeline_eu_payments",
        completed_at="2026-10-15T12:00:00+00:00", **kwargs,
    )


def test_rejected_report_contains_policy_and_simulation_without_claiming_commit():
    text = report([sample_log()])
    for value in ["BaFin Audit #88", "pipeline_eu_payments", "DEC-2026-409",
                  "DEC-2026-101", "15000", "140", "250",
                  "| Policy violation | Yes |", "| Simulation approved | No |",
                  "No execution was committed", "2026-10-15T12:00:00+00:00"]:
        assert value in text
    assert "not a regulatory compliance certification" in text


def test_policy_text_is_escaped_inside_markdown_table():
    text = report([sample_log()])
    assert "EU only &#124; no US<br>&lt;script&gt;unsafe&lt;/script&gt;" in text
    assert "<script>" not in text


def test_missing_lock_is_explicit_and_does_not_fabricate_agents():
    text = report([sample_log(True)], {"handoff_lock_id": "lock_123"})
    assert "Evidence incomplete" in text
    assert "Persisted graph trace" not in text
    assert "agent_planner" not in text


def test_missing_links_are_flagged():
    text = report([sample_log(True)], {"handoff_lock_id": "lock_123"}, [
        {"handoff_lock_id": "lock_123", "planner_id": None},
    ])
    assert "one or more provenance links are missing" in text


def test_lookup_is_bound_to_session_lock_shadow_and_episode():
    graph = Mock()
    graph.query.return_value.result_set = []
    assert fetch_handoff_provenance(graph, {
        "handoff_lock_id": "lock_123", "recorded_episode_id": "ep_123",
    }, sample_log(True)["simulation"]) == []
    assert graph.query.call_args.args[1] == {
        "lock_id": "lock_123", "shadow_id": "shadow_sim_123", "episode_id": "ep_123",
    }
