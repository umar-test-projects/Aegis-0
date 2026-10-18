"""Graph state and actual HTTP effects must agree before a recovery is verified."""

import pytest

from test_aegis_engine import live_engine


@pytest.fixture
def recovery(live_engine):
    from aegis_recovery import RecoveryCoordinator
    from aegis_runtime import DemoRuntime

    live_engine.seed_tri_layer_universe()
    with DemoRuntime() as runtime:
        yield RecoveryCoordinator(live_engine, runtime.client()), runtime


def approved(coordinator):
    action = coordinator.propose("pipeline_eu_payments", "pb_reroute_eu_sovereign")
    action = coordinator.audit(action["id"])
    assert action["status"] == "APPROVED"
    return action


def test_live_recovery_observes_http_before_reward_and_replays_once(recovery):
    coordinator, runtime = recovery
    action = approved(coordinator)
    assert coordinator.engine.recall_playbooks_and_company_brain("pipeline_eu_payments")[0]["playbook_id"] == "pb_reroute_us_east"
    result = coordinator.execute(action["id"])
    assert result["status"] == "VERIFIED"
    assert result["before"]["p95_ms"] >= 250
    assert result["observation"]["p95_ms"] < 250
    assert result["observation"]["errors"] == 0
    assert result["observation"]["service_ids"] == ["svc_eu_zurich_dr"]
    assert runtime.client().snapshot()["execution_count"] == 1
    assert coordinator.execute(action["id"]) == result
    assert runtime.client().snapshot()["execution_count"] == 1
    rows = coordinator.engine.graph.ro_query("MATCH (e:Episode {recovery_action_id: $id}) RETURN e.observed_latency_ms", {"id": action["id"]}).result_set
    assert rows[0][0] == pytest.approx(result["observation"]["p95_ms"])
    pipeline = coordinator.engine.graph.ro_query("MATCH (p:DataPipeline) RETURN p.latency_ms, p.observed_gateway_p95_ms").result_set[0]
    assert pipeline[0] == result["simulation"]["projected_e2e_latency_ms"]
    assert pipeline[1] == pytest.approx(result["observation"]["p95_ms"])


def test_live_changed_policy_blocks_action_without_touching_router(recovery):
    coordinator, runtime = recovery
    action = approved(coordinator)
    coordinator.engine.inject_regional_decision("pipeline_eu_payments", "DEC-new", "New approval required", True)
    result = coordinator.execute(action["id"])
    assert result["status"] == "STALE"
    assert runtime.client().snapshot()["execution_count"] == 0
    assert "outcome" not in result


def test_live_failed_gateway_cannot_earn_success_or_mark_graph_healthy(recovery):
    coordinator, runtime = recovery
    action = approved(coordinator)
    runtime.client().set_fault("svc_eu_zurich_dr", 0, fail=True)
    result = coordinator.execute(action["id"])
    assert result["status"] == "FAILED"
    assert result["observation"]["errors"] == 3
    assert runtime.client().snapshot()["target"] is None
    pipeline = coordinator.engine.graph.ro_query("MATCH (p:DataPipeline) RETURN p.status, p.latency_ms").result_set[0]
    assert pipeline == ["DEGRADED", 1420]
    assert coordinator.engine.graph.ro_query("MATCH (e:Episode {recovery_action_id: $id}) RETURN e.id", {"id": action["id"]}).result_set == []


def test_live_independent_auditor_and_restarted_executor_share_graph_state(recovery):
    from aegis_recovery import RecoveryCoordinator

    coordinator, runtime = recovery
    proposal = coordinator.propose("pipeline_eu_payments", "pb_reroute_eu_sovereign")
    auditor = RecoveryCoordinator(coordinator.engine, runtime.client())
    assert auditor.audit(proposal["id"])["status"] == "APPROVED"
    executor = RecoveryCoordinator(coordinator.engine, runtime.client())
    assert executor.execute(proposal["id"])["status"] == "VERIFIED"
    assert coordinator.get(proposal["id"])["status"] == "VERIFIED"


def test_live_rejected_route_and_all_blocked_incident_never_execute(recovery):
    coordinator, runtime = recovery
    coordinator.engine.inject_chaos_scenario("throughput_spike")
    result = coordinator.triage("pipeline_eu_payments")
    assert result["status"] == "BLOCKED"
    assert len(result["attempts"]) == 2
    assert all(row["status"] == "REJECTED" for row in result["attempts"])
    assert runtime.client().snapshot()["execution_count"] == 0


def test_live_restart_after_runtime_effect_resumes_without_duplicate_route(recovery):
    from aegis_recovery import RecoveryCoordinator

    coordinator, runtime = recovery
    action = approved(coordinator)
    coordinator.claim(action["id"])
    runtime.client().switch(action["id"], action["candidate"]["candidate_service_id"], action["runtime"]["revision"])
    restarted = RecoveryCoordinator(coordinator.engine, runtime.client())
    assert restarted.execute(action["id"])["status"] == "VERIFIED"
    assert runtime.client().snapshot()["execution_count"] == 1


def test_live_other_pending_execution_holds_pipeline_lease(recovery):
    from aegis_recovery import RecoveryBusy

    coordinator, runtime = recovery
    first, second = approved(coordinator), approved(coordinator)
    coordinator.claim(first["id"])
    with pytest.raises(RecoveryBusy):
        coordinator.execute(second["id"])
    assert runtime.client().snapshot()["execution_count"] == 0


def test_live_triage_recall_uses_persisted_learning_and_stops_when_healthy(recovery, monkeypatch):
    from aegis_recovery import RecoveryCoordinator

    coordinator, runtime = recovery
    result = coordinator.triage("pipeline_eu_payments")
    assert [row["candidate"]["playbook_id"] for row in result["attempts"]] == ["pb_reroute_us_east", "pb_reroute_eu_sovereign"]
    assert result["status"] == "VERIFIED"
    restarted = RecoveryCoordinator(coordinator.engine, runtime.client())
    assert restarted.engine.recall_playbooks_and_company_brain("pipeline_eu_payments")[0]["playbook_id"] == "pb_reroute_eu_sovereign"
    # Test the healthy-stop decision with the actual verified observation.
    # A new wall-clock sample can legitimately breach the SLA under host load,
    # in which case triage must attempt recovery instead of returning healthy.
    verified_observation = result["attempts"][-1]["observation"]
    monkeypatch.setattr(restarted.runtime, "measure", lambda: verified_observation)
    assert restarted.triage("pipeline_eu_payments")["status"] == "ALREADY_HEALTHY"
    assert runtime.client().snapshot()["execution_count"] == 1


def test_live_policy_change_during_measurement_disables_route_without_reward(recovery):
    coordinator, runtime = recovery
    action = approved(coordinator)
    original = coordinator.runtime

    class PolicyChangingObserver:
        def __getattr__(self, name):
            return getattr(original, name)

        def measure(self):
            observation = original.measure()
            coordinator.engine.inject_regional_decision("pipeline_eu_payments", "DEC-during", "Changed in flight", True)
            return observation

    coordinator.runtime = PolicyChangingObserver()
    result = coordinator.execute(action["id"])
    assert result["status"] == "STALE"
    assert runtime.client().snapshot()["target"] is None
    assert "outcome" not in result


def test_live_resume_after_graph_commit_does_not_reward_again(recovery, monkeypatch):
    from aegis_recovery import RecoveryCoordinator

    coordinator, runtime = recovery
    action = approved(coordinator)
    save = coordinator._save

    def crash_before_final_save(document, *args, **kwargs):
        if document["status"] == "VERIFIED":
            raise RuntimeError("executor stopped after graph commit")
        return save(document, *args, **kwargs)

    monkeypatch.setattr(coordinator, "_save", crash_before_final_save)
    with pytest.raises(RuntimeError, match="executor stopped"):
        coordinator.execute(action["id"])
    restarted = RecoveryCoordinator(coordinator.engine, runtime.client())
    assert restarted.get(action["id"])["status"] == "FINALIZING"
    result = restarted.execute(action["id"])
    assert result["status"] == "VERIFIED"
    assert runtime.client().snapshot()["execution_count"] == 1
    rows = coordinator.engine.graph.ro_query("MATCH (e:PlaybookWeightEvent {recovery_action_id: $id}) RETURN count(e)", {"id": action["id"]}).result_set
    assert rows == [[1]]


def test_live_approval_is_bound_to_the_original_runtime(recovery):
    from aegis_recovery import RecoveryCoordinator
    from aegis_runtime import DemoRuntime

    coordinator, runtime = recovery
    action = approved(coordinator)
    with DemoRuntime() as unrelated:
        other = RecoveryCoordinator(coordinator.engine, unrelated.client())
        assert other.execute(action["id"])["status"] == "STALE"
        assert unrelated.client().snapshot()["execution_count"] == 0
    assert runtime.client().snapshot()["execution_count"] == 0


def test_live_lineage_illustration_does_not_train_incident_memory(live_engine):
    from aegis_vector_rag_baseline import run_side_by_side_benchmark

    live_engine.seed_tri_layer_universe()
    result = run_side_by_side_benchmark(live_engine)
    assert result["simulation"]["simulation_approved"] is False
    assert live_engine.graph.ro_query("MATCH (e:PlaybookWeightEvent) RETURN count(e)").result_set == [[0]]
    assert live_engine.recall_playbooks_and_company_brain("pipeline_eu_payments")[0]["historical_success_rate"] == .94


def test_live_duplicate_workers_cannot_disable_each_others_successful_route(recovery):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from aegis_recovery import RecoveryBusy, RecoveryCoordinator

    coordinator, runtime = recovery
    action = approved(coordinator)
    first_switched, second_observing, first_finished = (threading.Event() for _ in range(3))
    client = runtime.client()

    class PausedObserver:
        def __init__(self, first):
            self.first = first

        def __getattr__(self, name):
            return getattr(client, name)

        def switch(self, *args):
            receipt = client.switch(*args)
            if self.first:
                first_switched.set()
            return receipt

        def measure(self):
            if self.first:
                assert second_observing.wait(10)
            else:
                second_observing.set()
                assert first_finished.wait(10)
            return client.measure()

    def execute_first():
        try:
            return RecoveryCoordinator(coordinator.engine, PausedObserver(True)).execute(action["id"])
        except RecoveryBusy:
            return None
        finally:
            first_finished.set()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(execute_first)
        assert first_switched.wait(10)
        second = workers.submit(RecoveryCoordinator(coordinator.engine, PausedObserver(False)).execute, action["id"])
        first.result(timeout=20)
        result = second.result(timeout=20)
    assert result["status"] == "VERIFIED"
    assert runtime.client().snapshot()["target"] == "svc_eu_zurich_dr"
    assert runtime.client().snapshot()["execution_count"] == 1


def test_live_policy_change_after_interrupted_http_effect_disables_old_route(recovery):
    coordinator, runtime = recovery
    action = approved(coordinator)
    coordinator.claim(action["id"])
    runtime.client().switch(action["id"], "svc_eu_zurich_dr", action["runtime"]["revision"])
    coordinator.engine.inject_regional_decision("pipeline_eu_payments", "DEC-after-crash", "Recheck policy", True)
    result = coordinator.execute(action["id"])
    assert result["status"] == "STALE"
    assert runtime.client().snapshot()["target"] is None
    assert "outcome" not in result


def test_live_stale_failure_worker_cannot_disable_a_verified_recovery(recovery):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from aegis_recovery import RecoveryCoordinator

    coordinator, runtime = recovery
    action = approved(coordinator)
    stopping, successful = threading.Event(), threading.Event()
    client = runtime.client()

    class DisconnectedObserver:
        def __getattr__(self, name):
            return getattr(client, name)

        def measure(self):
            raise OSError("Observer connection interrupted")

    class DelayedFailure(RecoveryCoordinator):
        stopping = False

        def _stop(self, *args, **kwargs):
            self.stopping = True
            return super()._stop(*args, **kwargs)

        def _committed(self, action_id):
            result = super()._committed(action_id)
            if self.stopping and result is None:
                stopping.set()
                assert successful.wait(15)
            return result

    with ThreadPoolExecutor(max_workers=1) as workers:
        failed_worker = workers.submit(DelayedFailure(coordinator.engine, DisconnectedObserver()).execute, action["id"])
        assert stopping.wait(10)
        try:
            assert coordinator.execute(action["id"])["status"] == "VERIFIED"
        finally:
            successful.set()
        assert failed_worker.result(timeout=20)["status"] == "VERIFIED"
    assert runtime.client().snapshot()["target"] == "svc_eu_zurich_dr"
    assert runtime.client().snapshot()["execution_count"] == 1


def test_live_repeated_measured_incident_keeps_float_snapshot_fresh(recovery):
    from aegis_demo import reset_incident

    coordinator, runtime = recovery
    assert coordinator.triage()["status"] == "VERIFIED"
    reset_incident(coordinator.engine, runtime.client())
    second = coordinator.triage()
    assert second["status"] == "VERIFIED"
    assert len(second["attempts"]) == 1
