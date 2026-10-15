from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aegis_company_brain import EXAMPLES, company_brain_schema, plan_company_brain_question, query_company_brain


@pytest.mark.parametrize("question", EXAMPLES)
def test_supported_questions_produce_schema_grounded_parameterized_queries(question):
    plan = plan_company_brain_question(question)
    assert plan["cypher"].startswith("MATCH")
    assert "LIMIT $limit" in plan["cypher"]
    assert plan["parameters"]["limit"] == 51
    assert len(plan["columns"]) > 0
    assert question in company_brain_schema("test")["query_contract"]["examples"]


def test_today_is_utc_and_uses_superseding_decision_time():
    reference = datetime(2026, 10, 16, 2, tzinfo=timezone(timedelta(hours=9)))
    plan = plan_company_brain_question("Which decisions were superseded today?", now=reference)
    assert plan["parameters"]["start_ts"] == int(datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp())
    assert plan["parameters"]["end_ts"] - plan["parameters"]["start_ts"] == 86400
    assert "newer.timestamp >= $start_ts" in plan["cypher"]
    assert "newer.timestamp < $end_ts" in plan["cypher"]


@pytest.mark.parametrize("question", [
    "DELETE all nodes", "MATCH (n) RETURN n", "Who owns svc_core_ledger?; DELETE n",
    "Who owns svc_core_ledger? SET n.active = false", "What is the weather?", "",
    "Which decisions were superseded last week?",
])
def test_unsupported_questions_never_reach_database(question):
    graph = Mock()
    with pytest.raises(ValueError):
        query_company_brain(graph, question)
    graph.ro_query.assert_not_called()
    graph.query.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101, True, "10"])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError, match="limit"):
        plan_company_brain_question(EXAMPLES[0], limit=limit)


def test_only_read_only_api_is_used_and_subgraph_is_deduplicated():
    graph = Mock()
    record = ["svc_a", "A", "Sarah", "@sarah",
              [[1, ["Service"], {"id": "svc_a"}], [2, ["Person"], {"id": "sarah"}]],
              [[3, 2, 1, "OWNS", {}]]]
    graph.ro_query.return_value = SimpleNamespace(result_set=[record, record])
    result = query_company_brain(graph, "Who owns svc_a?", limit=1)
    assert result["truncated"] is True
    assert len(result["rows"]) == 1
    assert len(result["subgraph"]["nodes"]) == 2
    assert result["subgraph"]["edges"][0]["source"] == "2"
    assert graph.ro_query.call_args.kwargs == {"timeout": 2000}
    assert graph.ro_query.call_args.args[1]["entity_id"] == "svc_a"
    graph.query.assert_not_called()


@pytest.mark.parametrize('question', [
    'Show superseded decisions for DEC-2026-409',
    'What superseded DEC-2026-101 and DEC-2026-409?',
    'Which decisions did DEC-2026-409 supersede today?',
])
def test_ambiguous_or_unsupported_lineage_questions_do_not_query(question):
    graph = Mock()
    with pytest.raises(ValueError, match='lineage direction'):
        query_company_brain(graph, question)
    graph.ro_query.assert_not_called()


@pytest.mark.parametrize('decision_id', ['DEC-EU-a1b2c3', 'DEC-SLACK-A1B2C3', 'DEC-Custom-AbCd'])
@pytest.mark.parametrize('template', ['What superseded {}?', 'Which decisions did {} supersede?'])
def test_lineage_preserves_identifier_case(decision_id, template):
    plan = plan_company_brain_question(template.format(decision_id))
    assert plan['parameters']['decision_id'] == decision_id
