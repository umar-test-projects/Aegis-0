"""Streamlit controls for measured local recovery and persisted handoffs."""

import atexit
import asyncio
import json
import os
import uuid

import streamlit as st

from aegis_agent import run_mcp_agent
from aegis_recovery import RecoveryCoordinator
from aegis_runtime import DemoRuntime, GATEWAYS, PRIMARY


def render_recovery_panel(engine):
    st.subheader("Measured incident recovery")
    st.caption("Synthetic local services · real HTTP requests · graph-checked approval · observed recovery")
    if "measured_runtime" not in st.session_state:
        if st.button("Start measured services", key="start_measured_services"):
            runtime = DemoRuntime()
            runtime.__enter__()
            atexit.register(runtime.__exit__, None, None, None)
            st.session_state.measured_runtime = runtime
            st.rerun()
        st.info("Start the local gateways to observe a slow route and run a verified recovery.")
        return

    runtime = st.session_state.measured_runtime
    client = runtime.client()
    coordinator = RecoveryCoordinator(engine, client)
    environment = {**os.environ, "AEGIS_GRAPH_NAME": engine.main_graph_name,
                   "AEGIS_RUNTIME_URL": runtime.url, "AEGIS_RUNTIME_TOKEN": runtime.token}
    state = client.snapshot()
    st.write(f"Router: `{runtime.url}` · Active gateway: `{state['target'] or 'DISABLED'}` · Revision: {state['revision']}")
    run, prepare, fault, reset = st.columns(4)
    try:
        if run.button("Run measured recovery", key="run_measured_recovery"):
            with st.spinner("Agent querying policies, auditing candidates and measuring recovery…"):
                st.session_state.measured_recovery_result = asyncio.run(run_mcp_agent(environment=environment))
            st.rerun()
        if prepare.button("Prepare handoff", key="prepare_measured_handoff"):
            with st.spinner("Planner and auditor preparing a persistent handoff…"):
                st.session_state.measured_recovery_result = asyncio.run(
                    run_mcp_agent(environment=environment, stop_after_approval=True))
            st.rerun()
        if fault.button("Fail EU backup", key="fail_measured_backup"):
            client.set_fault("svc_eu_zurich_dr", 0, fail=True)
            st.warning("EU backup will fail real requests. Its next execution must fail verification.")
        if reset.button("Reset graph + local incident", key="reset_measured_incident"):
            engine.seed_tri_layer_universe()
            for service, latency in GATEWAYS.items():
                client.set_fault(service, latency, fail=False)
            client.switch("reset_" + uuid.uuid4().hex, PRIMARY, client.snapshot()["revision"])
            for key in ("measured_recovery_result", "compliance_report", "last_outcome", "rag_benchmark"):
                st.session_state[key] = None
            st.session_state.simulation_logs = []
            st.rerun()

        actions = coordinator.list_actions()
        pending = [action for action in actions if action["status"] in {"APPROVED", "EXECUTING", "FINALIZING", "STOPPING"}]
        if pending:
            by_id = {action["id"]: action for action in pending}
            selected = st.selectbox("Pending handoff", list(by_id), key="pending_measured_handoff",
                format_func=lambda value: f"{by_id[value]['status']} · {value}")
            st.caption("The executor loads this handoff from FalkorDB. A policy update before execution requires a new approval.")
            if st.button("Execute / resume handoff", key="resume_measured_handoff"):
                with st.spinner("Independent executor checking approval and observing requests…"):
                    st.session_state.measured_recovery_result = asyncio.run(
                        run_mcp_agent(environment=environment, resume=selected))
                st.rerun()

        result = st.session_state.get("measured_recovery_result")
        if result:
            status = result["status"]
            display = st.success if status in {"VERIFIED", "ALREADY_HEALTHY"} else st.warning
            display(f"Recovery state: {status}")
            if result.get("tool_calls"):
                with st.expander("Agent tool calls", expanded=True):
                    st.dataframe(result["tool_calls"], hide_index=True)
            for action in result.get("attempts", []):
                with st.expander(f"{action['candidate']['playbook_name']} · {action['status']}", expanded=True):
                    st.code(action["id"], language=None)
                    simulation = action.get("simulation", {})
                    before, observed = action.get("before", {}), action.get("observation", {})
                    baseline, predicted, measured = st.columns(3)
                    baseline.metric("Before: measured HTTP p95", f"{before.get('p95_ms', 0):.1f} ms")
                    predicted.metric("Predicted dependency-path latency", f"{simulation.get('projected_e2e_latency_ms', 0):.1f} ms")
                    measured.metric("After: measured HTTP p95", f"{observed['p95_ms']:.1f} ms" if observed else "Not executed")
                    st.caption("HTTP measurements cover this local router and gateway. Dependency-path latency is the graph's separate model.")
                    if action.get("reason"):
                        st.write(action["reason"])
                    if simulation.get("rejection_reasons"):
                        st.write(simulation["rejection_reasons"])
                    st.write("Policy decisions:", simulation.get("governing_decisions", []))
                    if observed:
                        st.dataframe(observed["samples"], hide_index=True)
            st.download_button("Download measured recovery evidence", json.dumps(result, indent=2),
                               "aegis-measured-recovery.json", "application/json", key="download_measured_evidence")
        if actions:
            with st.expander("Persistent recovery journal"):
                st.dataframe([{"Action": action["id"], "Status": action["status"],
                               "Playbook": action["candidate"]["playbook_id"]} for action in actions], hide_index=True)
    except Exception as error:
        st.error(f"Measured recovery could not complete: {error}")
