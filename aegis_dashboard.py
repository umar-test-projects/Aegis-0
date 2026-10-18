import os
import json
import time
import uuid
import streamlit as st
import pandas as pd
from streamlit_agraph import agraph, Node, Edge, Config

from aegis_falkordb_engine import AegisGraphEngine
from aegis_company_brain import EXAMPLES, SCHEMA_URI, query_company_brain
from aegis_webhook_listener import AUTHORS, ingest_slack_message
from aegis_vector_rag_baseline import DEFAULT_QUERY, run_side_by_side_benchmark
from aegis_recovery_panel import render_recovery_panel
from aegis_compliance_report import (
    build_compliance_report, fetch_handoff_provenance, utc_now,
)

st.set_page_config(
    page_title="Aegis-0 | FalkorDB Counterfactual Governance Engine",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .block-container { padding-top: 1.2rem; padding-bottom: 1rem; }
    .track-badge-1 { background-color: #0e7490; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
    .track-badge-2 { background-color: #6d28d9; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
    .track-badge-3 { background-color: #b45309; color: white; padding: 3px 8px; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_engine() -> AegisGraphEngine:
    graph_engine = AegisGraphEngine(
        host=os.getenv("FALKORDB_HOST", "localhost"),
        port=int(os.getenv("FALKORDB_PORT", 6379)),
        password=os.getenv("FALKORDB_PASSWORD", None),
    )
    if os.getenv("AEGIS_GRAPH_NAME"):
        graph_engine.main_graph_name = os.environ["AEGIS_GRAPH_NAME"]
        graph_engine.graph = graph_engine.db.select_graph(graph_engine.main_graph_name)
    return graph_engine


engine = get_engine()


@st.fragment(run_every=5)
def render_playbook_memory():
    st.subheader("📈 Procedural Memory: Playbook Weight Evolution")
    tracked = ["pb_reroute_us_east", "pb_reroute_eu_sovereign"]
    try:
        history = [row for row in engine.get_playbook_weight_history() if row["playbook_id"] in tracked]
        candidates = engine.recall_playbooks_and_company_brain("pipeline_eu_payments")
    except Exception:
        st.info("Playbook memory is unavailable. Check the database connection.")
        return
    if not history:
        st.info("Seed the graph to start tracking playbook weights.")
        return
    journal = pd.DataFrame(history)
    incidents = {incident: index for index, incident in enumerate(journal["incident_id"].drop_duplicates())}
    journal["Incident run"] = journal["incident_id"].map(incidents)
    trend = journal.pivot_table(index="Incident run", columns="playbook_id", values="weight", aggfunc="last").sort_index().ffill()
    st.line_chart(trend, y_label="Learned weight (0–1)")
    current = {candidate["playbook_id"]: candidate["historical_success_rate"]
               for candidate in candidates if candidate["playbook_id"] in tracked}
    if current:
        st.bar_chart(pd.DataFrame.from_dict(current, orient="index", columns=["Active-context weight"]))
    st.caption(
        "Updates every 5 seconds. Rejections multiply the applicable policy-context weight by 0.8; "
        "committed successes reward that same context. Lines show persisted updates grouped by incident "
        "in first-seen order; unattempted playbooks retain their previous plotted value. "
        "Bars show current-context weights. New policy contexts start from seeded baselines."
    )
    with st.expander("Persisted memory event journal"):
        st.json(history)

if "initialized" not in st.session_state:
    try:
        res = engine.graph.query("MATCH (n) RETURN count(n)")
        if res.result_set[0][0] == 0:
            engine.seed_tri_layer_universe()
    except Exception:
        st.error("Unable to initialize the dashboard. Check the database connection and reload.")
        st.stop()
    st.session_state.initialized = True
    st.session_state.simulation_logs = []
    st.session_state.last_outcome = None

st.session_state.setdefault("compliance_report", None)
st.session_state.setdefault("rag_benchmark", None)
st.session_state.setdefault("chaos_event", None)
st.session_state.setdefault("company_brain_history", [])
st.session_state.setdefault("last_slack_ingestion", None)


def fetch_live_pipeline_metrics(pipeline_id: str = "pipeline_eu_payments"):
    cypher = """
    MATCH (p:DataPipeline {id: $pipeline_id})-[r:ROUTES_THROUGH {active: true}]->(gw:Service)-[:LOCATED_IN]->(reg:Region)
    OPTIONAL MATCH (p)<-[:DEPENDS_ON*1..3]-(down:Service)-[:BOUND_BY]->(sla:SLA)
    OPTIONAL MATCH (dec:Decision {active: true})-[:GOVERNS]->(p)
    OPTIONAL MATCH (owner:Person)-[owns:OWNS]->(p)
    RETURN
        p.status, p.latency_ms, gw.id, gw.name, reg.id, reg.gdpr_compliant,
        sla.max_allowed_latency_ms, sla.penalty_per_min_usd,
        dec.id, dec.summary, dec.forbids_non_eu, dec.source_url,
        owner.slack_handle, owns.expertise_score
    ORDER BY sla.max_allowed_latency_ms ASC, owns.expertise_score DESC LIMIT 1
    """
    res = engine.graph.query(cypher, {"pipeline_id": pipeline_id})
    if not res.result_set:
        return None
    row = res.result_set[0]
    return {
        "status": row[0],
        "latency_ms": row[1],
        "active_gateway_id": row[2],
        "active_gateway_name": row[3],
        "region_id": row[4],
        "gdpr_compliant": row[5],
        "sla_max_ms": row[6],
        "sla_penalty_usd": row[7],
        "active_decision_id": row[8],
        "active_decision_summary": row[9],
        "forbids_non_eu": row[10],
        "decision_source_url": row[11],
        "owner_handle": row[12],
        "owner_score": row[13],
    }


def fetch_agraph_elements(layer_filter: str = "All 3 Layers"):
    nodes_dict = {}
    edges_list = []

    color_map = {
        "DataPipeline": "#ef4444",
        "Service": "#06b6d4",
        "Region": "#3b82f6",
        "SLA": "#f43f5e",
        "Agent": "#8b5cf6",
        "Playbook": "#10b981",
        "Episode": "#a855f7",
        "RejectedEpisode": "#ef4444",
        "PlaybookContext": "#14b8a6",
        "PlaybookWeightEvent": "#22c55e",
        "HandoffLock": "#ec4899",
        "Person": "#f59e0b",
        "Decision": "#f97316",
    }

    track1_labels = {"DataPipeline", "Service", "Region", "SLA"}
    track2_labels = {"Agent", "Playbook", "Episode", "RejectedEpisode", "PlaybookContext", "PlaybookWeightEvent", "HandoffLock"}
    track3_labels = {"Person", "Decision"}

    cypher = """
    MATCH (n)-[r]->(m)
    RETURN
        id(n), labels(n)[0], coalesce(n.name, n.id), properties(n),
        type(r), properties(r),
        id(m), labels(m)[0], coalesce(m.name, m.id), properties(m)
    """
    res = engine.graph.query(cypher)

    for row in res.result_set:
        n_id, n_label, n_title, n_props = str(row[0]), row[1], row[2], row[3]
        r_type, r_props = row[4], row[5]
        m_id, m_label, m_title, m_props = str(row[6]), row[7], row[8], row[9]

        if layer_filter == "Track 1: Connected Topology" and not (
            n_label in track1_labels and m_label in track1_labels
        ):
            continue
        if layer_filter == "Track 2: Agent Memory & Coordination" and not (
            n_label in track2_labels or m_label in track2_labels
        ):
            continue
        if layer_filter == "Track 3: Company Brain" and not (
            n_label in track3_labels or m_label in track3_labels
        ):
            continue

        for node_uid, lbl, title, props in [
            (n_id, n_label, n_title, n_props),
            (m_id, m_label, m_title, m_props),
        ]:
            if node_uid not in nodes_dict:
                bg_color = color_map.get(lbl, "#64748b")
                if lbl == "DataPipeline" and props.get("status") == "HEALTHY":
                    bg_color = "#22c55e"
                elif lbl == "Decision" and not props.get("active", True):
                    bg_color = "#475569"

                tooltip = f"[{lbl}] {title}\n" + "\n".join(
                    [f"{k}: {v}" for k, v in props.items()]
                )
                nodes_dict[node_uid] = Node(
                    id=node_uid,
                    label=f"{lbl}:\n{str(title)[:22]}",
                    size=26 if lbl in ("DataPipeline", "Decision", "HandoffLock") else 20,
                    color=bg_color,
                    title=tooltip,
                )

        edge_color = "#94a3b8"
        if r_type == "ROUTES_THROUGH" and r_props.get("active") is True:
            edge_color = "#22c55e"
        elif r_type == "SUPERSEDES":
            edge_color = "#f97316"
        elif r_type in ("INITIATED_HANDOFF", "VERIFIED_HANDOFF", "AUTHORIZED_EXECUTION"):
            edge_color = "#ec4899"

        edges_list.append(
            Edge(
                source=n_id,
                target=m_id,
                label=r_type,
                color=edge_color,
            )
        )

    return list(nodes_dict.values()), edges_list


def snapshot_agraph_elements(snapshot):
    """Render a JSON subgraph without making another database query."""
    palette = {"DataPipeline": "#ef4444", "Service": "#06b6d4", "Region": "#3b82f6",
               "SLA": "#f43f5e", "Decision": "#f97316", "Person": "#f59e0b"}
    prefix = snapshot["graph_id"] + ":"
    nodes = []
    for node in snapshot["nodes"]:
        props = node["properties"]
        label = node["labels"][0] if node["labels"] else "Node"
        nodes.append(Node(
            id=prefix + node["id"], label=f"{label}:\n{props.get('name', props.get('id', node['id']))}",
            title=json.dumps(props, indent=2), color=palette.get(label, "#64748b"), size=26,
        ))
    edges = []
    for edge in snapshot["edges"]:
        simulated_route = edge["type"] == "ROUTES_THROUGH" and edge["properties"].get("simulated") is True
        edges.append(Edge(
            source=prefix + edge["source"], target=prefix + edge["target"],
            label=edge["type"] + (" (simulated)" if simulated_route else ""),
            color="#facc15" if simulated_route else "#f97316" if edge["type"] == "SUPERSEDES" else "#94a3b8",
            width=5 if simulated_route else 1,
            title=json.dumps(edge["properties"], indent=2),
        ))
    return nodes, edges


def inject_slack_decision(decision_id: str, summary: str, forbids_non_eu: bool):
    st.session_state.rag_benchmark = None
    engine.inject_regional_decision(
        "pipeline_eu_payments", decision_id, summary, forbids_non_eu,
        source_url="https://slack.internal/archives/C02compliance/live_override",
    )
    st.session_state.simulation_logs = []
    st.session_state.last_outcome = None
    st.session_state.compliance_report = None


with st.sidebar:
    st.title("🎛️ Chaos & Policy Deck")
    st.caption("Use these controls during your live demo to mutate the FalkorDB graph.")

    if st.button("🔄 1. Reset & Seed Tri-Layer Graph", use_container_width=True):
        engine.seed_tri_layer_universe()
        st.session_state.simulation_logs = []
        st.session_state.last_outcome = None
        st.session_state.compliance_report = None
        st.session_state.rag_benchmark = None
        st.session_state.chaos_event = None
        st.session_state.company_brain_history = []
        st.session_state.last_slack_ingestion = None
        st.session_state.measured_recovery_result = None
        st.toast("Tri-Layer Universe freshly seeded in FalkorDB!", icon="✅")
        st.rerun()

    st.divider()
    st.subheader("🏢 Enterprise Failure Scenarios")
    chaos_scenario = st.selectbox(
        "Chaos scenario", options=list(engine.CHAOS_SCENARIOS),
        format_func=lambda key: engine.CHAOS_SCENARIOS[key],
        key="chaos_scenario_selector",
    )
    st.caption("Inject into the live graph and evaluate both failover playbooks. Scenarios accumulate; reset the graph for an isolated demo.")
    apply_chaos = st.button("Inject Chaos & Evaluate", use_container_width=True)
    if apply_chaos:
        st.session_state.chaos_event = engine.inject_chaos_scenario(chaos_scenario)
        st.session_state.simulation_logs = []
        st.session_state.last_outcome = None
        st.session_state.compliance_report = None
        st.session_state.rag_benchmark = None
    if st.session_state.chaos_event:
        st.info(st.session_state.chaos_event["description"])

    st.divider()
    st.subheader("⚡ Track 3: Live Company Brain")
    st.caption("Inject a new Slack policy node (`[:SUPERSEDES]`) and watch how the agent adapts its routing decision.")

    with st.form("slack_thread_form"):
        slack_author = st.selectbox("Slack author", AUTHORS, key="slack_author")
        slack_text = st.text_area(
            "Simulate Incoming Slack Thread", key="slack_thread_text",
            placeholder="Urgent: Freeze all traffic to Zurich DR node due to maintenance",
        )
        submit_slack = st.form_submit_button("Simulate Incoming Slack Thread")
    st.caption("Use one existing target and an explicit freeze or resume directive. Service freezes preserve the pipeline's regional policy.")
    if submit_slack:
        try:
            st.session_state.last_slack_ingestion = ingest_slack_message(engine, slack_text, slack_author)
            st.session_state.rag_benchmark = None
            st.session_state.simulation_logs = []
            st.session_state.last_outcome = None
            st.session_state.compliance_report = None
            st.rerun()
        except ValueError as error:
            st.error(str(error))
        except Exception:
            st.error("Slack ingestion failed. Check the database connection and retry.")
    if st.session_state.last_slack_ingestion:
        thread = st.session_state.last_slack_ingestion
        st.success(f"{thread['decision_id']} · {thread['action']} · {thread['target_id']} · {thread['author']}")
        st.caption("Superseded: " + (", ".join(thread["superseded_decision_ids"]) or "No prior decision on this target"))
        st.caption("Run Shadow-Graph triage to evaluate the new directive.")

    col_p1, col_p2 = st.columns(2)
    with col_p1:
        if st.button("🔒 Enforce EU GDPR Freeze", use_container_width=True):
            inject_slack_decision(
                decision_id=f"DEC-EU-{uuid.uuid4().hex}",
                summary="STRICT FREEZE: EU payment streams must remain strictly within GDPR-compliant EU regions.",
                forbids_non_eu=True,
            )
            st.toast("Injected EU GDPR Freeze Decision via [:SUPERSEDES]!", icon="🔒")
            st.rerun()

    with col_p2:
        if st.button("🌐 Lift EU Freeze (Allow US)", use_container_width=True):
            inject_slack_decision(
                decision_id=f"DEC-GLOBAL-{uuid.uuid4().hex}",
                summary="EMERGENCY WAIVER: Cross-Atlantic US failover authorized for low-latency recovery.",
                forbids_non_eu=False,
            )
            st.toast("Injected Global Failover Waiver via [:SUPERSEDES]!", icon="🌐")
            st.rerun()

    st.divider()
    st.subheader("🚀 Autonomous Multi-Agent Loop")
    run_triage = st.button(
        "🧠 Run Shadow-Graph Triage & Self-Heal",
        type="primary",
        use_container_width=True,
    )
    run_triage = run_triage or apply_chaos

    st.divider()
    layer_filter = st.radio(
        "🔍 Filter Graph Visualization by Track:",
        [
            "All 3 Layers",
            "Track 1: Connected Topology",
            "Track 2: Agent Memory & Coordination",
            "Track 3: Company Brain",
        ],
    )


st.title("🛡️ Aegis-0: Counterfactual Shadow-Graph Governance Engine")
st.markdown(
    '<span class="track-badge-1">Track 01: Connected Data & Blast Radius</span> &nbsp;'
    '<span class="track-badge-2">Track 02: Episodic/Procedural Memory & Handoffs</span> &nbsp;'
    '<span class="track-badge-3">Track 03: Company Brain & Decision Lineage</span>',
    unsafe_allow_html=True,
)

render_recovery_panel(engine)
st.divider()
st.caption("Graph state below includes modeled dependency-path checks. Use measured recovery above for local HTTP evidence.")
metrics = fetch_live_pipeline_metrics("pipeline_eu_payments")

if metrics:
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(
        "Pipeline Status (`pipeline_eu_payments`)",
        metrics["status"],
        delta="SLA BREACH" if metrics["latency_ms"] > metrics["sla_max_ms"] else "SLA HEALTHY",
        delta_color="inverse" if metrics["latency_ms"] > metrics["sla_max_ms"] else "normal",
    )
    m2.metric(
        "Current E2E Latency",
        f"{metrics['latency_ms']} ms",
        delta=f"Max Allowed: {metrics['sla_max_ms']} ms (${metrics['sla_penalty_usd']:,}/min risk)",
        delta_color="off",
    )
    m3.metric(
        "Active Gateway Route",
        metrics["active_gateway_id"],
        delta=f"Region: {metrics['region_id']} (GDPR: {metrics['gdpr_compliant']})",
        delta_color="off",
    )
    m4.metric(
        "Active Company Brain Policy",
        metrics["active_decision_id"],
        delta=f"Forbids Non-EU: {metrics['forbids_non_eu']} | Owner: {metrics['owner_handle']}",
        delta_color="off",
    )

st.divider()

if run_triage:
    st.session_state.compliance_report = None
    target_pipeline = "pipeline_eu_payments"
    candidates = engine.recall_playbooks_and_company_brain(target_pipeline)
    sim_logs = []
    outcome = None
    provenance = []
    provenance_error = None
    incident_id = f"incident_{uuid.uuid4().hex}"

    for idx, cand in enumerate(candidates, 1):
        sim_report = engine.run_shadow_graph_simulation(
            pipeline_id=target_pipeline,
            candidate_service_id=cand["candidate_service_id"],
            playbook_id=cand["playbook_id"], incident_id=incident_id,
        )
        sim_logs.append({"candidate": cand, "simulation": sim_report})

        if sim_report["simulation_approved"] and outcome is None:
            try:
                outcome = engine.coordinate_handoff_and_commit(
                    pipeline_id=target_pipeline,
                    playbook_id=cand["playbook_id"],
                    sim_report=sim_report,
                    expert_handle=cand["expert_handle"],
                    policy_source=cand["policy_source"],
                )
            except ValueError as error:
                st.session_state.simulation_logs = sim_logs
                st.session_state.last_outcome = None
                st.warning(str(error))
                st.stop()
            try:
                provenance = fetch_handoff_provenance(engine.graph, outcome, sim_report)
            except Exception:
                provenance_error = "Matching graph provenance unavailable"
            break

    st.session_state.simulation_logs = sim_logs
    st.session_state.last_outcome = outcome
    if sim_logs:
        st.session_state.compliance_report = build_compliance_report(
            sim_logs, outcome, provenance, pipeline_id=target_pipeline,
            completed_at=utc_now(), provenance_error=provenance_error,
        )
    st.rerun()


st.download_button(
    "📥 Export BaFin Audit #88 Markdown report",
    data=st.session_state.compliance_report or "",
    file_name="aegis_bafin_audit_88_report.md",
    mime="text/markdown",
    disabled=not st.session_state.compliance_report,
    help="Download simulation and handoff evidence from the latest triage in this session.",
)
if not st.session_state.compliance_report:
    st.caption("Run Shadow-Graph triage to prepare a compliance evidence report.")


inspect_shadow = st.toggle("🔍 Inspect Shadow Sandbox Diff", key="inspect_shadow_diff")
if inspect_shadow:
    col_graph, col_sandbox = st.columns(2, gap="large")
    col_trace = st.container()
else:
    col_graph, col_trace = st.columns([1.25, 1.0], gap="large")

with col_graph:
    st.subheader("🕸️ Live FalkorDB Tri-Layer Graph Topology")
    st.caption(
        "🔴 Red/Cyan: Service & SLA Topology (Track 1) | 🟣 Purple/Green: Agent Playbooks & Handoff Locks (Track 2) | 🟠 Orange/Amber: Company Brain Decisions & Owners (Track 3)"
    )

    nodes, edges = fetch_agraph_elements(layer_filter)
    config = Config(
        width="100%",
        height=560,
        directed=True,
        physics=True,
        hierarchical=False,
        nodeHighlightBehavior=True,
        highlightColor="#facc15",
        collapsible=False,
    )
    agraph(nodes=nodes, edges=edges, config=config)

if inspect_shadow:
    with col_sandbox:
        st.subheader("🔬 Captured Shadow Sandbox")
        reports = [entry["simulation"] for entry in st.session_state.simulation_logs]
        benchmark = st.session_state.rag_benchmark
        if benchmark and benchmark.get("simulation"):
            reports.append(benchmark["simulation"])
        snapshots = {report["shadow_graph_id"]: report for report in reports
                     if report.get("shadow_snapshot")}
        if snapshots:
            selected_id = st.selectbox(
                "Shadow simulation attempt", options=list(snapshots),
                index=len(snapshots) - 1,
            )
            selected = snapshots[selected_id]
            snapshot = selected["shadow_snapshot"]
            st.caption(
                f"{selected_id} · target {selected['target_service']} · "
                f"{'Approved' if selected['simulation_approved'] else 'Rejected'}"
            )
            st.caption(
                "Yellow: counterfactual ROUTES_THROUGH {simulated: true}. "
                "This saved subgraph shows the sandbox before deletion; production on the left is live."
            )
            shadow_nodes, shadow_edges = snapshot_agraph_elements(snapshot)
            agraph(nodes=shadow_nodes, edges=shadow_edges, config=Config(
                width="100%", height=560, directed=True, physics=True,
                hierarchical=False, nodeHighlightBehavior=True,
            ))
            st.download_button(
                "Download shadow snapshot JSON", data=json.dumps(snapshot, indent=2),
                file_name=f"{selected_id}.json", mime="application/json",
            )
        else:
            st.info("Run Shadow-Graph triage or the side-by-side benchmark to capture a sandbox snapshot.")

with col_trace:
    st.subheader("🔬 Traceable Agent Execution & Shadow Simulation")

    tab_sim, tab_rag, tab_cypher = st.tabs(
        [
            "🧪 Shadow-Graph Audit Log",
            "Policy lineage illustration",
            "📜 Live Cypher Provenance",
        ]
    )

    with tab_sim:
        if not st.session_state.simulation_logs:
            st.info(
                "Click **'🧠 Run Shadow-Graph Triage & Self-Heal'** in the sidebar to watch the Planner, RiskAuditor, and Executor agents coordinate via FalkorDB."
            )
        else:
            for step_idx, item in enumerate(st.session_state.simulation_logs, 1):
                cand = item["candidate"]
                sim = item["simulation"]

                with st.expander(
                    f"Attempt #{step_idx}: {cand['playbook_name']} (Prior Success: {cand['historical_success_rate']*100:.1f}%)",
                    expanded=True,
                ):
                    st.markdown(
                        f"**1. Procedural Memory Recall (Track 2):** Selected `{cand['playbook_id']}` targeting `{cand['candidate_service_id']}`."
                    )
                    st.markdown(
                        f"**2. Company Brain Lineage Check (Track 3):** Found active policy `{cand['active_decision_id']}` (supersedes `{cand['superseded_decision_id']}`): *\"{cand['active_policy_summary']}\"*"
                    )
                    st.markdown(
                        f"**3. Ephemeral Shadow-Graph Fork (Track 1):** Cloned subgraph into `{sim['shadow_graph_id']}` and tested counterfactual route to `{sim['region']}`."
                    )
                    if sim.get("capacity_checks"):
                        st.markdown("**4. Multi-hop capacity and SLA constraints**")
                        st.write(
                            f"Required throughput: {sim['required_throughput_tps']:,.0f} TPS · "
                            f"Capacity passed: {sim['capacity_passed']} · SLA passed: {sim['sla_passed']}"
                        )
                        st.dataframe(sim["capacity_checks"], hide_index=True)
                        st.dataframe(sim["sla_checks"], hide_index=True)
                        with st.expander("Dependency paths evaluated"):
                            st.json(sim["dependency_paths"])
                    if sim.get("policy_checks"):
                        with st.expander("Applicable policy evidence"):
                            st.json(sim["policy_checks"])
                    if sim.get("rejection_memory"):
                        st.caption("RiskAuditor recorded rejection episodes and applied a 20% penalty in this policy context.")
                        st.json(sim["rejection_memory"])

                    if not sim["simulation_approved"]:
                        st.error(
                            f"❌ **SHADOW SIMULATION REJECTED BY AUDITOR**\n\n"
                            f"- **Target Region:** `{sim['region']}`\n"
                            f"- **Policy Violation:** `{sim['policy_violation']}` (Decision `{sim['governing_decision']}`)\n"
                            f"- **Reasons:** {'; '.join(sim.get('rejection_reasons', []))}\n"
                            f"- **Action:** Sandbox `{sim['shadow_graph_id']}` destroyed. No handoff authorized for this attempt."
                        )
                    else:
                        st.success(
                            f"✅ **SHADOW SIMULATION PASSED & APPROVED**\n\n"
                            f"- **Target Region:** `{sim['region']}` (Governing policy passed)\n"
                            f"- **Projected E2E Latency:** `{sim['projected_e2e_latency_ms']} ms` (<= SLA Limit `{sim['sla_limit_ms']} ms`)\n"
                            f"- **Penalty Avoided:** `${sim['penalty_at_risk_usd']:,}/min`"
                        )

            if st.session_state.last_outcome:
                out = st.session_state.last_outcome
                st.markdown("### 🤝 Multi-Agent Handoff & Self-Healing Committed")
                st.json(out)

    with tab_rag:
        st.markdown("#### Policy lineage: lexical retrieval and graph verification")
        st.caption(
            "Text-only demo: TF-IDF vectors with cosine similarity over the two seeded "
            "Jira/Slack summaries. Aegis queries the live graph and checks US failover "
            "in an ephemeral shadow graph. No production route is changed."
        )
        st.caption("These are different tasks: your query affects lexical scoring; the graph always checks the EU payments pipeline. This is not a general RAG accuracy or speed benchmark.")
        benchmark_query = st.text_input("Incident retrieval query", value=DEFAULT_QUERY)
        if st.button("Run Side-by-Side Benchmark", disabled=not benchmark_query.strip()):
            st.session_state.rag_benchmark = None
            try:
                with st.spinner("Retrieving policy evidence and checking US failover…"):
                    st.session_state.rag_benchmark = run_side_by_side_benchmark(
                        engine, benchmark_query
                    )
            except Exception:
                st.error("Benchmark failed. Check the FalkorDB connection and seeded topology, then retry.")

        benchmark = st.session_state.rag_benchmark
        if benchmark:
            st.caption(f"Results for: {benchmark['query']}")
            vector_column, graph_column = st.columns(2)
            with vector_column:
                st.markdown("##### Cosine-similarity Vector RAG")
                st.metric("Vector retrieval time", f"{benchmark['vector_ms']:.3f} ms")
                for rank, hit in enumerate(benchmark["vector_results"], 1):
                    st.markdown(f"**#{rank} — {hit['decision_id']}**")
                    st.write(f"{hit['source']} · cosine similarity {hit['cosine_similarity']:.4f}")
                    st.write(hit["text"])
                    st.caption(hit["source_url"])
                top = benchmark["vector_results"][0] if benchmark["vector_results"] else None
                superseded = {row["superseded_decision_id"] for row in benchmark["graph_results"]}
                if top and top["cosine_similarity"] > 0 and top["decision_id"] in superseded:
                    st.error(
                        f"Top text match {top['decision_id']} is superseded in the live graph. "
                        "Following its US failover instruction would use an outdated rule."
                    )
                else:
                    st.info("Cosine ranking alone does not establish whether a decision is current.")
            with graph_column:
                st.markdown("##### Aegis-0 live graph retrieval")
                st.metric("Graph retrieval time", f"{benchmark['graph_ms']:.3f} ms")
                decisions = {row["active_decision_id"]: row for row in benchmark["graph_results"]
                             if row["active_decision_id"]}
                for decision_id, row in decisions.items():
                    st.markdown(f"**Active decision: {decision_id}**")
                    st.write(row["active_policy_summary"])
                    st.caption(row["policy_source"])
                    if row["superseded_decision_id"]:
                        st.code(f"{decision_id} -[:SUPERSEDES]-> {row['superseded_decision_id']}")
                st.code("WHERE NOT (dec)<-[:SUPERSEDES]-()", language="cypher")
                sim = benchmark["simulation"]
                if sim:
                    st.metric("US shadow verification time", f"{benchmark['simulation_ms']:.3f} ms")
                    st.write(f"US target: {sim['target_service']} · region: {sim['region']}")
                    st.write(f"SLA passed: {sim['sla_passed']} · capacity passed: {sim['capacity_passed']} · policy violation: {sim['policy_violation']}")
                    if sim["policy_violation"] and not sim["simulation_approved"]:
                        st.success(f"US failover BLOCKED by {sim['governing_decision']}. Compliance breach prevented.")
                    elif sim["simulation_approved"]:
                        st.warning(f"US failover allowed by current policy {sim['governing_decision']}. The EU freeze may have been lifted.")
                    else:
                        st.warning("US failover rejected: " + "; ".join(sim.get("rejection_reasons", [])))
                    st.caption(f"Ephemeral sandbox {sim['shadow_graph_id']} deleted after verification.")
                else:
                    st.warning("No governed US failover candidate found. Reset and seed the graph to run the default scenario.")
            st.caption(
                "Single-run wall-clock timings, not a speed comparison: vector indexing is excluded, "
                "graph retrieval includes a database round trip, and shadow verification is timed separately. "
                "Rejected verification also updates RiskAuditor procedural memory. "
                "The default incident query illustrates stale-policy retrieval; different queries can rank Slack first."
            )
        st.markdown("##### Capability comparison")
        comparison_df = pd.DataFrame(
            [
                {
                    "Capability": "Policy Contradiction Handling",
                    "Standard Vector RAG": "Ranks raw decision text by similarity without resolving superseded policies.",
                    "Aegis-0 (FalkorDB)": "Traverses (:Decision)-[:SUPERSEDES]->(:Decision) and deterministically filters out superseded rules.",
                },
                {
                    "Capability": "Downstream SLA Blast Radius",
                    "Standard Vector RAG": "Cannot trace 3-hop service call chains across separate architecture docs.",
                    "Aegis-0 (FalkorDB)": "Executes multi-hop [:DEPENDS_ON*1..3] traversal to compute exact E2E latency and $15k/min SLA exposure.",
                },
                {
                    "Capability": "Pre-Execution Safety",
                    "Standard Vector RAG": "Text retrieval alone does not verify a proposed production change.",
                    "Aegis-0 (FalkorDB)": "Tests counterfactual routes in isolated Shadow Graphs and rejects policy violations.",
                },
                {
                    "Capability": "Cross-Session Learning",
                    "Standard Vector RAG": "Static embeddings; fails to update playbook weights after execution.",
                    "Aegis-0 (FalkorDB)": "Writes (:Episode) and (:HandoffLock) nodes and recalculates (:Playbook) success_rate weights live.",
                },
            ]
        )
        st.table(comparison_df)

    with tab_cypher:
        st.markdown("#### Core Multi-Hop & Temporal Cypher Queries Executed")
        st.code(
            """// 1. Track 2 + Track 3: Procedural Playbook + Active Decision Lineage
MATCH (p:DataPipeline {id: 'pipeline_eu_payments'})<-[:APPLIES_TO]-(pb:Playbook)
OPTIONAL MATCH (dec:Decision)-[:GOVERNS]->(p)
WHERE NOT (dec)<-[:SUPERSEDES]-() // Deterministically ignore superseded policies
OPTIONAL MATCH (owner:Person)-[owns:OWNS]->(p)
RETURN pb.id, pb.success_rate, dec.id, dec.forbids_non_eu, owner.slack_handle
ORDER BY pb.success_rate DESC, owns.expertise_score DESC;

// 2. Track 1: Counterfactual Shadow-Graph Verification
MATCH (sla:SLA)<-[:BOUND_BY]-(down:Service)-[:DEPENDS_ON]->(p:DataPipeline)
      -[:ROUTES_THROUGH {simulated: true}]->(cand:Service)-[:LOCATED_IN]->(reg:Region)
MATCH (dec:Decision)-[:GOVERNS]->(p)
RETURN (cand.base_latency_ms + down.base_latency_ms + 30) <= sla.max_allowed_latency_ms AS sla_ok,
       (dec.forbids_non_eu AND NOT reg.gdpr_compliant) AS policy_violation;""",
            language="cypher",
        )


st.divider()
render_playbook_memory()
st.divider()
st.subheader("💬 Ask the Company Brain")
st.caption(
    f"Ask about ownership, active policies, or supersession history using {SCHEMA_URI}. "
    "Questions use schema-based read-only templates. Today and yesterday mean UTC."
)
with st.expander("Example questions"):
    for example in EXAMPLES:
        st.write(example)

question = st.chat_input("Ask about owners, policies or decision lineage", key="company_brain_question")
if question:
    entry = {"question": question}
    try:
        with st.spinner("Querying Company Brain…"):
            entry["answer"] = query_company_brain(engine.graph, question)
    except ValueError as error:
        entry["error"] = str(error)
    except Exception:
        entry["error"] = "Company Brain query failed. Check the database connection and retry."
    st.session_state.company_brain_history = (st.session_state.company_brain_history + [entry])[-10:]

for entry in st.session_state.company_brain_history:
    with st.chat_message("user"):
        st.write(entry["question"])
    with st.chat_message("assistant"):
        if "error" in entry:
            st.info(entry["error"])
            continue
        answer = entry["answer"]
        st.write(answer["explanation"])
        st.caption(f"Schema: {answer['schema_uri']} · Queried at {answer['queried_at_utc']}")
        st.code(answer["cypher"], language="cypher")
        with st.expander("Query parameters"):
            st.json(answer["parameters"])
        if answer["rows"]:
            st.dataframe(answer["rows"], hide_index=True)
            brain_nodes, brain_edges = snapshot_agraph_elements(answer["subgraph"])
            agraph(nodes=brain_nodes, edges=brain_edges, config=Config(
                width="100%", height=380, directed=True, physics=True, hierarchical=False,
            ))
        else:
            st.info("No matching records. Historical seed decisions may fall outside today's UTC window.")
        if answer["truncated"]:
            st.warning(f"Showing the first {answer['limit']} rows and their subgraph.")
