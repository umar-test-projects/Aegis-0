"""Run or evaluate measured local recovery using disposable demo graphs."""

import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from aegis_falkordb_engine import AegisGraphEngine
from aegis_recovery import PIPELINE, RecoveryCoordinator
from aegis_runtime import DemoRuntime, GATEWAYS, PRIMARY


def validate_demo_graph(name):
    if not re.fullmatch(r"aegis_(?:demo|test)_[A-Za-z0-9_]+", name):
        raise ValueError("Demo commands require an aegis_demo_* or aegis_test_* graph")
    return name


def demo_engine(name):
    validate_demo_graph(name)
    engine = AegisGraphEngine(host=os.getenv("FALKORDB_HOST", "127.0.0.1"),
                             port=int(os.getenv("FALKORDB_PORT", "6379")),
                             password=os.getenv("FALKORDB_PASSWORD"))
    engine.main_graph_name = name
    engine.graph = engine.db.select_graph(name)
    return engine


def reset_incident(engine, client):
    """Inject another synthetic incident while preserving learned memory."""
    validate_demo_graph(engine.main_graph_name)
    engine.graph.query("""
        MATCH (p:DataPipeline {id: 'pipeline_eu_payments'}),
              (primary:Service {id: 'svc_eu_gateway_primary'}),
              (eu:Service {id: 'svc_eu_zurich_dr'}), (us:Service {id: 'svc_us_failover_fast'})
        SET p.status = 'DEGRADED', p.latency_ms = 1420,
            primary.current_load_pct = 94, eu.current_load_pct = 31, us.current_load_pct = 22
        WITH p, primary
        OPTIONAL MATCH (p)-[old:ROUTES_THROUGH {active: true}]->()
        SET old.active = false
        WITH DISTINCT p, primary
        CREATE (p)-[:ROUTES_THROUGH {active: true, load_contribution_pct: 37.5}]->(primary)
    """)
    for service, latency in GATEWAYS.items():
        client.set_fault(service, latency, fail=False)
    client.switch("incident_" + uuid.uuid4().hex, PRIMARY, client.snapshot()["revision"])


def _approve(coordinator):
    proposal = coordinator.propose(PIPELINE, "pb_reroute_eu_sovereign")
    return coordinator.audit(proposal["id"])


def evaluate():
    results = []
    cases = ("recovery", "stale_policy", "all_blocked", "failed_gateway",
             "resumed_executor", "duplicate_execution", "policy_during_action", "memory_ablation")
    for case in cases:
        engine = demo_engine("aegis_test_evaluation_" + uuid.uuid4().hex)
        try:
            with redirect_stdout(sys.stderr):
                engine.seed_tri_layer_universe()
            with DemoRuntime() as runtime:
                coordinator = RecoveryCoordinator(engine, runtime.client())
                expected, checks = "VERIFIED", {}
                if case == "recovery":
                    evidence = coordinator.triage()
                    checks["one_route_change"] = runtime.client().snapshot()["execution_count"] == 1
                elif case == "all_blocked":
                    engine.inject_chaos_scenario("throughput_spike")
                    evidence, expected = coordinator.triage(), "BLOCKED"
                    checks["no_route_change"] = runtime.client().snapshot()["execution_count"] == 0
                elif case == "memory_ablation":
                    first = coordinator.triage()
                    reset_incident(engine, runtime.client())
                    learned = RecoveryCoordinator(engine, runtime.client()).triage()
                    reset_incident(engine, runtime.client())
                    baseline = coordinator.triage(use_memory=False)
                    evidence = {"status": learned["status"], "first_incident": first,
                                "with_memory": learned, "without_memory": baseline,
                                "attempts_with_memory": len(learned["attempts"]),
                                "attempts_without_memory": len(baseline["attempts"])}
                    checks["fewer_attempts_on_repeated_fixture"] = len(learned["attempts"]) < len(baseline["attempts"])
                else:
                    action = _approve(coordinator)
                    if case == "stale_policy":
                        engine.inject_regional_decision(PIPELINE, "DEC-eval-new", "Approval invalidated", True)
                        expected = "STALE"
                    elif case == "failed_gateway":
                        runtime.client().set_fault("svc_eu_zurich_dr", 0, True)
                        expected = "FAILED"
                    elif case == "resumed_executor":
                        coordinator.claim(action["id"])
                        runtime.client().switch(action["id"], "svc_eu_zurich_dr", action["runtime"]["revision"])
                        coordinator = RecoveryCoordinator(engine, runtime.client())
                    elif case == "policy_during_action":
                        expected = "STALE"
                        original = coordinator.runtime

                        class ChangingObserver:
                            def __getattr__(self, name):
                                return getattr(original, name)

                            def measure(self):
                                observation = original.measure()
                                engine.inject_regional_decision(PIPELINE, "DEC-eval-flight", "Changed in flight", True)
                                return observation

                        coordinator.runtime = ChangingObserver()
                    evidence = coordinator.execute(action["id"])
                    if case == "stale_policy":
                        checks["no_route_change"] = runtime.client().snapshot()["execution_count"] == 0
                    if case in {"failed_gateway", "policy_during_action"}:
                        checks["router_disabled"] = runtime.client().snapshot()["target"] is None
                        checks["no_success_reward"] = coordinator._committed(action["id"]) is None
                    if case == "duplicate_execution":
                        replay = coordinator.execute(action["id"])
                        checks["same_receipt"] = replay == evidence
                        checks["one_route_change"] = runtime.client().snapshot()["execution_count"] == 1
                status = evidence["status"]
                checks["expected_state"] = status == expected
                results.append({"case": case, "expected": expected, "status": status,
                                "passed": all(checks.values()), "checks": checks, "evidence": evidence})
        except Exception as error:
            results.append({"case": case, "passed": False, "error": str(error)})
        finally:
            engine.graph.delete()
    return {"scope": "Synthetic local HTTP services; graph dependency latency is modeled separately",
            "generated_at_unix": time.time(), "case_count": len(results),
            "passed": sum(case["passed"] for case in results), "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", default="aegis_demo_verified")
    parser.add_argument("--reset", action="store_true", help="Reset only the named demo graph")
    parser.add_argument("--seed-only", action="store_true")
    parser.add_argument("--resume", help="Resume a durable recovery action")
    parser.add_argument("--state", default="scratch/demo-runtime.json")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--output", help="Write complete JSON evidence to this file")
    args = parser.parse_args()
    if args.evaluate:
        result = evaluate()
        exit_code = 0 if result["passed"] == result["case_count"] else 1
    else:
        engine = demo_engine(args.graph)
        if args.reset:
            with redirect_stdout(sys.stderr):
                engine.seed_tri_layer_universe()
        elif engine.graph.query("MATCH (n) RETURN count(n)").result_set[0][0] == 0:
            with redirect_stdout(sys.stderr):
                engine.seed_tri_layer_universe()
        if args.seed_only:
            result = {"status": "SEEDED", "graph": args.graph}
        else:
            with DemoRuntime(state_path=args.state) as runtime:
                if args.reset:
                    reset_incident(engine, runtime.client())
                coordinator = RecoveryCoordinator(engine, runtime.client())
                result = coordinator.execute(args.resume) if args.resume else coordinator.triage()
        exit_code = 0 if result["status"] in {"VERIFIED", "ALREADY_HEALTHY", "SEEDED"} else 1
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps({"evidence_file": str(output.resolve()),
                          "status": result.get("status"), "passed": result.get("passed"),
                          "case_count": result.get("case_count")}))
    else:
        print(json.dumps(result, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
