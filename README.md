# Aegis-0

A FalkorDB governance and self-healing demo combining service dependency
analysis, ephemeral shadow simulations, policy lineage, and procedural memory.
Includes a Streamlit dashboard, MCP server, Slack webhook listener, and
Markdown audit evidence export.

## Setup and tests

Start a local FalkorDB instance and run the unit and live integration tests:

```bash
python -m pip install -r requirements.txt
docker compose up -d --wait
AEGIS_TEST_FALKORDB=1 pytest test_aegis_engine.py -v
```

Live tests create unique `aegis_test_*` graphs and delete them after each test.
They never seed or delete the production graph. To use an existing FalkorDB,
skip the Compose command and set `FALKORDB_HOST`, `FALKORDB_PORT`, and optionally
`FALKORDB_PASSWORD`. An explicitly enabled live test fails if the database is
unavailable. Without `AEGIS_TEST_FALKORDB=1`, only unit tests run.

The ranking uses directed service-to-service `DEPENDS_ON` edges. Pipeline nodes
and routing relationships are excluded. Scores average normalized incoming degree
and directed betweenness; they represent structural risk rather than a count of
all services affected by an outage.

Stop a database started through Compose with `docker compose down`.

## Dashboard compliance export

```bash
streamlit run aegis_dashboard.py
```

Run **Shadow-Graph Triage & Self-Heal**, then click **Export BaFin Audit #88
Markdown report**. The report captures all simulation attempts from the latest
run in your dashboard session, policy lineage and citations, SLA checks, commit
results, and the matching persisted HandoffLock/agent/episode/playbook trace.
It remains available across reruns; resetting the graph clears it. Missing graph
provenance is explicitly marked as incomplete. An all-rejected run reports that
no execution was committed.

Run all tests, including report formatting and a live simulation-to-handoff export:

```bash
AEGIS_TEST_FALKORDB=1 pytest -v
```

## Live Vector RAG comparison

In **⚔️ Why Vector RAG Fails Here**, click **Run Side-by-Side Benchmark**.
The default incident query ranks the raw Jira `DEC-2026-101` US failover summary
above Slack `DEC-2026-409` using local TF-IDF/cosine similarity. The graph side
executes the engine's live `SUPERSEDES`-filtered retrieval, then verifies the US
route in a disposable shadow graph. The seeded EU freeze rejects that route.
Lifting the freeze changes the displayed result to reflect the current policy.

Both retrieval times are measured with `perf_counter` in milliseconds; shadow
verification is timed separately. Vector indexing is excluded and graph retrieval
includes a database round trip. This two-chunk lexical simulation illustrates
missing policy lineage; it is not a general embedding-model performance benchmark.
Queries mentioning GDPR or BaFin can rank the Slack rule first. No handoff is
committed and no production route is changed by the benchmark.

## Inspect a shadow sandbox

Run triage or the benchmark, then enable **🔍 Inspect Shadow Sandbox Diff**.
Choose an approved or rejected simulation to view its captured sandbox beside
the current live production graph. The simulated `ROUTES_THROUGH` edge is yellow
and thicker than other edges. This is a captured counterfactual subgraph, rather
than a full production graph copy; production may already reflect a committed
triage action. The sidebar layer filter applies to the production visualization.

Each simulation returns `shadow_snapshot` with `graph_id`, `nodes` (ID, labels,
properties), and `edges` (ID, source, target, type, properties). The dashboard's
**Download shadow snapshot JSON** button exports that snapshot. Sandbox data is
captured before deletion, and verification or export failures still trigger
cleanup. No shadow database needs to remain running for inspection.

## Enterprise chaos scenarios

Reset and seed once to install the service throughput profiles. Select a **Chaos
scenario** in the sidebar, then click **Inject Chaos & Evaluate**. Injection
mutates the live graph and automatically evaluates the failover playbooks.
Scenarios accumulate; reset before each one to reproduce isolated outcomes.

| Scenario | Injected failure | Expected checks |
| --- | --- | --- |
| Cascading ledger lock | Locked EU ledger, US replica in lock wait, US settlement API depending on the replica | Lock waits propagate across dependency paths; locked/overloaded services and downstream SLA breaches reject failover |
| Throughput spike | Pipeline demand rises from 4,500 to 18,000 TPS; Zurich DR load rises to 95% | Zurich's current and projected load exceed the 90% ceiling |
| SLA upgrade | Adds a concurrent Platinum SLA: 100 ms limit, 10,000 TPS minimum, $75,000/min penalty | Original and upgraded contracts are both checked; the faster US route violates EU policy and the latency limit, while Zurich also exceeds capacity |

The sandbox preserves dependency edges up to four hops instead of flattening
the chain. Every service and attached SLA on these paths is evaluated. Projected
latency sums service latency and lock waits along each path plus the candidate
gateway latency. Capacity uses an explicit demo model: incoming candidate traffic
adds to its current background utilization; services already carrying the pipeline
use the larger of current load and required TPS as a fraction of capacity. Exactly
90% is allowed; missing capacity profiles fail the check. SLA minimum TPS raises
the required throughput for the shared route.

The audit log displays capacity tables, per-contract SLA results, dependency
paths, and rejection reasons. These are also retained in sandbox JSON and the
Markdown report. Rejected simulations cannot authorize a handoff.

## Ask the Company Brain

The dashboard's **💬 Ask the Company Brain** panel accepts questions such as:

- Who owns services impacted by BaFin Audit #88?
- Which decisions were superseded today?
- Which decisions were superseded yesterday?
- What are the active policies?
- Who owns svc_merchant_checkout?
- What superseded DEC-2026-101?
- Which decisions did DEC-2026-409 supersede?

Translation uses deterministic, parameterized intent templates grounded in the
same schema definition exposed by `falkordb://aegis/schema`; no model service or
API key is required. Unsupported questions return examples instead of guessed
Cypher. Results show the actual Cypher, bound parameters, evidence table, and
returned subgraph. The latest ten exchanges remain in the session until reset.

Audit ownership queries traverse dependent services within four hops under
currently applicable audit decisions. They preserve audit context across an
uninterrupted chain of inherited regional restrictions (`forbids_non_eu=true`),
returning both the active decision and the original audit decision with lineage
in the subgraph. A waiver breaks inheritance, so later generic regional freezes
do not silently reactivate an old audit. Direct service owners and pipeline owners
are separate fields. Historical queries retain superseded decisions; current
policy queries exclude decisions with incoming `SUPERSEDES` edges. Today and
yesterday use UTC boundaries and the superseding decision's Unix timestamp.
Seeded historical records may produce no results for today's window.
“What superseded DEC-2026-101?” returns successors; “Which decisions did
DEC-2026-409 supersede?” returns predecessors. Both directions include paths up
to four hops. Copy decision IDs exactly as displayed: identifier case is preserved,
while surrounding question wording is matched without regard to case. Ambiguous
or unsupported lineage phrasing is rejected before querying.

Agents can call the new `query_company_brain_cypher(question, limit=50)` MCP tool,
which returns the same JSON evidence and subgraph. Queries execute through
`GRAPH.RO_QUERY` with a two-second timeout and at most 100 result rows. Truncated
results are marked. Raw Cypher and mutation requests are not accepted.

```bash
python aegis_mcp_server.py
```

## Incoming Slack threads

In the dashboard sidebar, choose **Slack author**, type into **Simulate Incoming
Slack Thread**, and submit. For example:

```text
Urgent: Freeze all traffic to Zurich DR node due to maintenance
```

The parser resolves one existing Service or DataPipeline by ID, name, or known
alias (Zurich, core ledger, checkout, EU payments). It recognizes explicit
freeze/block/stop and resume/unfreeze/unblock directives. Unsupported actions,
questions, negation and ambiguous targets return an error without writing.
The author must be `@marcus.compliance` or `@sarah.chen`; an optional matching
author prefix in the raw text is supported.

Ingestion creates a Decision with the raw text, resolved target, source and time,
links the existing Person via `AUTHORED`, and links the target via `GOVERNS`.
Previously current decisions for that same target become inactive and receive
`SUPERSEDES` links from the new decision. The first decision on a service has no
predecessor. Pipeline EU restrictions are inherited; resuming a service never
grants a cross-region waiver. Run triage to see the resulting traffic freeze
enforced across the candidate and all services on the dependency paths. Snapshot
JSON and Markdown reports include the applicable Slack policy evidence.

For local webhook simulations, start the listener (default loopback port 8080):

```bash
python aegis_webhook_listener.py
curl http://127.0.0.1:8080/slack/events \
  -H 'Content-Type: application/json' \
  -d '{"author":"@marcus.compliance","text":"Freeze Zurich DR node for maintenance","event_id":"demo-zurich-freeze-1"}'
```

For Slack Events API delivery, configure `SLACK_SIGNING_SECRET` and
`AEGIS_SLACK_USER_MAP` (a JSON object mapping real Slack user IDs to the two allowed
handles). The listener verifies Slack HMAC signatures and the five-minute request
window, handles URL verification, ignores bot/edit events, and reads the sender
from the mapped event user ID. Signed mode accepts Slack Events payloads rather
than local simulation JSON. Use `--host` and `--port` to configure the bind address.

Slack `event_id` values deduplicate retries, including retries after a later
decision has superseded the original directive. Reusing an ID with different
content fails. The listener uses the same `FALKORDB_HOST`, `FALKORDB_PORT`, and
`FALKORDB_PASSWORD` settings as the dashboard; seed the graph before ingestion.

## Policy-scoped procedural learning

Every rejected Shadow-Graph simulation now records a `RejectedEpisode` through
`(RiskAuditor)-[:RECORDED]->(RejectedEpisode)-[:USED_PLAYBOOK]->(Playbook)`, including
the rejection reasons, shadow graph ID, incident ID, and governing decisions.
This happens after sandbox cleanup, for dashboard, MCP, CLI, and benchmark calls.
Pass `playbook_id` to identify the attempted playbook; when omitted, matching
playbooks for the pipeline and candidate route are inferred. Passing the same
report again to `record_rejected_episode` does not apply a second penalty.

Each pipeline and sorted set of applicable current decisions defines a policy
context. A `PlaybookContext` stores its independent `success_rate`, run count,
and rejection count. Rejections multiply that weight by **0.8** and increment the
run count. A committed success applies `(weight * runs + 1) / (runs + 1)` in that
same context. Passing a simulation alone earns no reward. These adaptive weights
are ranking signals, rather than empirical probabilities after penalty decay.

`Playbook.success_rate` projects the last updated context for graph inspection.
Recall always resolves the currently applicable context, including candidate and
dependency service policies. A new context starts from the unchanged historical
baseline (US: 0.94 over 18 runs; EU: 0.89 over 9). Old context penalties remain
auditable and do not penalize a newly authorized policy context.

The dashboard's **Procedural Memory: Playbook Weight Evolution** section refreshes
every five seconds. The line chart uses persisted `PlaybookWeightEvent` history
grouped by incident, and the bar chart shows both playbooks' current-context
weights. Its journal includes context IDs and policy decision IDs. Dashboard
triage groups related attempts under one incident ID; chart refreshes and ordinary
page reruns do not create training events. The side-by-side benchmark does run a
new simulation and therefore records a rejection when the US route is blocked.

To see both weights decay across runs, inject the throughput-spike scenario and
run triage repeatedly. To see successful learning and candidate reordering, reset
and run default triage: the first rejected US route drops from 0.94 to 0.752, while
the committed EU route rises from 0.89 to 0.901. Subsequent runs prefer the EU route.

## Commit freshness and policy updates

Simulation reports capture the operational nodes and relationships used to
verify a route. Commit compares that snapshot with the live graph inside the
same database query that changes the route. Changed capacity, throughput, locks,
SLAs, policies, or topology require a new simulation. This check conservatively
covers the full operational graph, so unrelated operational changes can also
invalidate a report. Retrying an already completed commit returns its original
receipt without changing routing or rewarding the playbook again.

Active routing edges record `load_contribution_pct`. Switching routes releases
that contribution from the departing gateway; subsequent simulations distinguish
background utilization from this pipeline's traffic. For older edges without the
field, the first departure estimates the contribution from current pipeline
throughput and gateway capacity. That migration is an estimate if historical
traffic differed; new edges retain the explicitly calculated contribution.

Dashboard and MCP regional-policy updates share one atomic implementation. Each
update creates one successor linked to all previous active decisions, preserves
any pipeline traffic freeze, and rejects an already-used decision ID. Resume
traffic explicitly through the Slack directive flow to clear a maintenance
freeze. Slack messages must begin with an explicit action (optionally preceded
by `Urgent:` or `Please`); negated and narrative messages are rejected.

A dashboard initialization error now displays a connection error and stops.
Only a successful empty-graph check or the explicit reset control seeds data.
