import hashlib
import hmac
import json
from http.server import HTTPServer
from threading import Thread
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from aegis_webhook_listener import make_handler, parse_slack_message, verify_slack_signature


TARGETS = [
    {"id": "svc_eu_zurich_dr", "name": "EU Sovereign DR Node"},
    {"id": "svc_core_ledger", "name": "Global Settlement Ledger"},
    {"id": "pipeline_eu_payments", "name": "EU Real-Time Payment Stream"},
]


def test_raw_author_prefixed_thread_resolves_example():
    result = parse_slack_message(
        "@marcus.compliance: Urgent: Freeze all traffic to Zurich DR node due to maintenance",
        None, TARGETS,
    )
    assert result["author"] == "@marcus.compliance"
    assert result["target_id"] == "svc_eu_zurich_dr"
    assert result["traffic_blocked"] is True


@pytest.mark.parametrize("text,target,blocked", [
    ("Resume all traffic to Zurich", "svc_eu_zurich_dr", False),
    ("Lift the freeze on svc_core_ledger", "svc_core_ledger", False),
    ("Freeze the EU payment pipeline", "pipeline_eu_payments", True),
    ("Block svc_core_ledger due to lock contention", "svc_core_ledger", True),
])
def test_target_and_action_resolution(text, target, blocked):
    result = parse_slack_message(text, "@sarah.chen", TARGETS)
    assert result["target_id"] == target
    assert result["traffic_blocked"] is blocked


@pytest.mark.parametrize("text,author", [
    ("Freeze Zurich and ledger", "@sarah.chen"),
    ("Do not freeze Zurich", "@sarah.chen"),
    ("Should we freeze Zurich?", "@sarah.chen"),
    ("Freeze Zurich unless payments recover", "@sarah.chen"),
    ("Freeze then resume Zurich", "@sarah.chen"),
    ("Freeze the mystery database", "@sarah.chen"),
    ("Zurich maintenance tomorrow", "@sarah.chen"),
    ("Freeze Zurich", "@unknown"),
    ("@sarah.chen: Freeze Zurich", "@marcus.compliance"),
])
def test_ambiguous_or_unauthorized_directives_are_rejected(text, author):
    with pytest.raises(ValueError):
        parse_slack_message(text, author, TARGETS)


def test_slack_signatures_and_replay_window():
    body, timestamp, secret = b'{"type":"url_verification"}', "1000", "test-secret"
    signature = "v0=" + hmac.new(secret.encode(), b"v0:1000:" + body, hashlib.sha256).hexdigest()
    assert verify_slack_signature(body, timestamp, signature, secret, now=1001)
    assert not verify_slack_signature(body + b" ", timestamp, signature, secret, now=1001)
    assert not verify_slack_signature(body, timestamp, signature, secret, now=1301)
    assert not verify_slack_signature(body, None, signature, secret, now=1001)


def test_signed_listener_rejects_unsigned_request_before_database_access():
    engine = Mock()
    server = HTTPServer(("127.0.0.1", 0), make_handler(engine, signing_secret="test-secret"))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/slack/events",
                          data=json.dumps({"author": "@sarah.chen", "text": "Freeze Zurich"}).encode(),
                          headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=3)
        assert error.value.code == 401
        engine.graph.query.assert_not_called()
        engine.graph.ro_query.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize('message', [
    'Don’t freeze Zurich', "Don't freeze Zurich", 'No need to freeze Zurich',
    'We discussed whether to freeze Zurich', 'Please do not resume Zurich',
])
def test_negated_and_narrative_messages_do_not_become_directives(message):
    with pytest.raises(ValueError):
        parse_slack_message(message, '@marcus.compliance', TARGETS)
