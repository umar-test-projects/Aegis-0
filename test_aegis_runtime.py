"""Real loopback requests catch missing execution, replay and control boundaries."""

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


def test_route_change_improves_observed_http_requests():
    from aegis_runtime import DemoRuntime

    with DemoRuntime() as runtime:
        client = runtime.client()
        before = client.measure(samples=2)
        assert before["p95_ms"] >= 250
        receipt = client.switch("repair-1", "svc_eu_zurich_dr", 0)
        after = client.measure(samples=2)
        assert receipt["revision"] == 1
        assert after["service_ids"] == ["svc_eu_zurich_dr"]
        assert after["errors"] == 0
        assert after["p95_ms"] < 250


def test_duplicate_action_returns_receipt_without_switching_again(tmp_path):
    from aegis_runtime import DemoRuntime, RuntimeConflict

    state = tmp_path / "runtime.json"
    with DemoRuntime(state_path=state) as runtime:
        first = runtime.client().switch("repair-1", "svc_eu_zurich_dr", 0)
        assert runtime.client().switch("repair-1", "svc_eu_zurich_dr", 0) == first
        with pytest.raises(RuntimeConflict):
            runtime.client().switch("repair-1", "svc_us_failover_fast", 0)
    with DemoRuntime(state_path=state) as restarted:
        assert restarted.client().switch("repair-1", "svc_eu_zurich_dr", 0) == first
        assert restarted.client().snapshot()["revision"] == 1


def test_stale_revision_and_unknown_target_cannot_change_route():
    from aegis_runtime import DemoRuntime, RuntimeConflict, RuntimeControlError

    with DemoRuntime() as runtime:
        client = runtime.client()
        client.switch("first", "svc_eu_zurich_dr", 0)
        with pytest.raises(RuntimeConflict):
            client.switch("second", "svc_us_failover_fast", 0)
        with pytest.raises(RuntimeControlError):
            client.switch("third", "http://example.com", 1)
        assert client.snapshot()["revision"] == 1
        assert client.snapshot()["target"] == "svc_eu_zurich_dr"


def test_unauthorized_control_does_not_mutate_runtime():
    from aegis_runtime import DemoRuntime

    with DemoRuntime() as runtime:
        request = Request(runtime.url + "/control/route", method="POST",
                          data=json.dumps({"action_id": "bad", "target": "svc_eu_zurich_dr",
                                           "expected_revision": 0}).encode(),
                          headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=2)
        assert error.value.code == 401
        assert runtime.client().snapshot()["revision"] == 0


def test_failed_gateway_is_an_observed_failure_and_block_disables_requests():
    from aegis_runtime import DemoRuntime

    with DemoRuntime() as runtime:
        client = runtime.client()
        client.set_fault("svc_eu_zurich_dr", latency_ms=0, fail=True)
        client.switch("repair", "svc_eu_zurich_dr", 0)
        assert client.measure(samples=2)["errors"] == 2
        client.switch("stop", None, 1)
        assert client.measure(samples=1)["errors"] == 1
        assert client.snapshot()["target"] is None


@pytest.mark.parametrize("url", ["https://127.0.0.1:8787", "http://example.com", "http://127.0.0.1.evil.test", "http://127.0.0.1:8787/path"])
def test_client_rejects_nonliteral_loopback_endpoints(url):
    from aegis_runtime import RuntimeClient

    with pytest.raises(ValueError):
        RuntimeClient(url, "test-token")
