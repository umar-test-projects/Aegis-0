# Verified local recovery

The user approved the incident-recovery roadmap and requested implementation.
The primary demonstration must perform real HTTP routing against explicitly local,
synthetic services, validate policy and topology in FalkorDB, and verify observed
results before recording success. No deployment or outbound messaging is required.

## Architecture

- `aegis_runtime.py`: loopback-only HTTP gateways and router, authenticated control,
  persistent idempotent route receipts, route revisions, measured request samples.
  Gateway identities match existing Service IDs. A disabled route returns 503.
- `aegis_recovery.py`: separate propose, audit, execute/resume operations. Actions,
  simulations, before/after measurements and tool traces persist in FalkorDB.
  A pipeline lease prevents competing approved actions from executing together.
  Freshness is checked when claiming execution and again after observation.
- Existing graph commit is reused after successful observation, with a distinct
  observed-latency field. Existing graph-only demos remain explicitly model-based.
- MCP exposes each phase independently. A bounded controller and CLI use the same
  operations; their graph-selected decisions and rejection reasons are visible.
- Streamlit provides the measured recovery workflow, separate from graph-only
  simulation; documentation includes schema, commands, evidence and a video script.

## State and safety

Actions move PROPOSED -> APPROVED/REJECTED -> EXECUTING -> VERIFIED/FAILED/STALE.
Before terminal effects, FINALIZING and STOPPING claim the current action version.
Success commit checks FINALIZING and that version atomically; compensation claims
STOPPING before disabling the router. These phases are also resumable.
There is one persisted simulator per control endpoint and a unique execution ID
per action. A retry uses the original receipt. Restarting a coordinator must not
re-execute an already applied route or reward it twice. A policy/topology change
before execution yields STALE without touching the runtime. Changes during action
or failed verification disable the local router and record the reason. There is
no distributed transaction across HTTP and FalkorDB; this boundary is documented.

Runtime control accepts only known gateways, bounded request bodies, finite
latencies, and a per-instance bearer token. Control calls do not follow redirects.
The client accepts literal loopback addresses only. No arbitrary destination URL
is accepted through graph data or an MCP tool argument.

## Acceptance evidence

Tests must cover measured recovery, rejected/all-blocked routes, changed policy
after audit, failing gateway, restart/resume, conflicting actions, duplicate
execution, and memory changing subsequent candidate order. Simulator HTTP tests
run without FalkorDB; graph tests use unique disposable `aegis_test_*` graphs.
Full existing tests must continue to pass. Evaluation outputs report measured
results and failures, and never manufacture a winning percentage.

The existing two-chunk retrieval illustration is relabeled as policy lineage,
with its unequal retrieval tasks disclosed. This increment does not claim a
general embedding benchmark or independently running LLM agents.
