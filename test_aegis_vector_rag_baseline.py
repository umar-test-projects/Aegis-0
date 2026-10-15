import math
from unittest.mock import Mock

import pytest

from aegis_vector_rag_baseline import (
    DEFAULT_QUERY, RAW_DECISION_CHUNKS, VectorRAGBaseline, run_side_by_side_benchmark,
)


def test_default_incident_retrieves_old_jira_rule_first():
    hits = VectorRAGBaseline().retrieve(DEFAULT_QUERY)
    assert [hit["decision_id"] for hit in hits] == ["DEC-2026-101", "DEC-2026-409"]
    assert hits[0]["cosine_similarity"] > hits[1]["cosine_similarity"] > 0
    assert hits[0]["text"] == RAW_DECISION_CHUNKS[0]["text"]


def test_different_query_can_rank_slack_first():
    hit = VectorRAGBaseline().retrieve("GDPR compliant BaFin Audit STRICT FREEZE", top_k=1)[0]
    assert hit["decision_id"] == "DEC-2026-409"


def test_cosine_matches_hand_calculated_tfidf_vectors():
    baseline = VectorRAGBaseline([
        {"decision_id": "a", "text": "failover eu"},
        {"decision_id": "b", "text": "eu"},
    ])
    hits = baseline.retrieve("failover")
    idf = math.log(3 / 2) + 1
    assert hits[0]["cosine_similarity"] == pytest.approx(idf / math.sqrt(idf**2 + 1))
    assert hits[1]["cosine_similarity"] == 0


@pytest.mark.parametrize("query", ["", "UNKNOWNWORD"])
def test_empty_or_out_of_vocabulary_queries_have_zero_scores(query):
    assert all(hit["cosine_similarity"] == 0 for hit in VectorRAGBaseline().retrieve(query))


def test_empty_index_and_zero_limit():
    assert VectorRAGBaseline([]).retrieve("failover") == []
    assert VectorRAGBaseline().retrieve(DEFAULT_QUERY, top_k=0) == []


def test_benchmark_does_not_simulate_when_no_governed_candidate_exists():
    engine = Mock()
    engine.recall_playbooks_and_company_brain.return_value = []
    result = run_side_by_side_benchmark(engine)
    assert result["simulation"] is None
    assert result["simulation_ms"] is None
    assert result["vector_ms"] >= 0 and result["graph_ms"] >= 0
    engine.run_shadow_graph_simulation.assert_not_called()
    engine.coordinate_handoff_and_commit.assert_not_called()


def test_database_errors_are_not_replaced_by_synthetic_results():
    engine = Mock()
    engine.recall_playbooks_and_company_brain.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        run_side_by_side_benchmark(engine)
