# Verified Recovery Implementation Plan

**Goal:** Demonstrate graph-approved local HTTP recovery with observed outcomes.
**Spec:** `docs/superpowers/specs/2026-10-18-verified-recovery-design.md`
**Architecture:** Standard-library local simulator, persisted FalkorDB action
state, independently callable audit/execution phases, MCP and dashboard adapters.
**Tech:** Python, existing FalkorDB client, Streamlit, FastMCP, pytest.

## Global constraints

Loopback-only simulator. Parameterized graph writes. Preserve sandbox cleanup and
SUPERSEDES filters. Preserve existing graph-only demo compatibility and label it.
Only a passing observed recovery may earn a reward in the new execution path.
No external notification or production infrastructure effects.

## Review focus

- Stale policy before/after HTTP action must never produce a verified recovery.
- Duplicate/resumed execution must not repeat route mutation or learning reward.
- Concurrent pipeline proposals must not silently overwrite each other's route.
- Malformed or unauthorized runtime controls must not mutate state.
- Failed/time-out/wrong-route observations must not mark the pipeline healthy.

## Task 1: Local HTTP runtime

Create `aegis_runtime.py`, `test_aegis_runtime.py`.
Produce `DemoRuntime(state_path=None, port=0)` context manager, `RuntimeClient`,
`snapshot()`, `switch(action_id, target, expected_revision)`, `measure(samples=3)`,
`set_fault(service_id, latency_ms, fail=False)`. Three gateway IDs match the seed.
Write HTTP tests first: degraded-to-healthy measured requests, receipt replay,
conflicting revision, persisted receipts, unauthorized controls, unknown gateways.
Run `.venv/Scripts/python.exe -m pytest test_aegis_runtime.py -q` RED then GREEN.

## Task 2: Persistent recovery coordinator

Create `aegis_recovery.py`, `test_aegis_recovery.py`; extend engine commit with an
optional observed latency. Produce `RecoveryCoordinator(engine, runtime)` with
`propose(pipeline_id, playbook_id)`, `audit(action_id)`, `execute(action_id)`,
`get(action_id)`, `triage(pipeline_id)`. Persist action JSON/status/version and
pipeline execution leases; bind audits to simulator revision and graph snapshot.
Live tests use the existing disposable engine fixture pattern. Write failure
cases first, run them RED, implement, run both new files plus full suite GREEN.

## Task 3: Agent tools, CLI and evaluation

Extend `aegis_mcp_server.py` with separate recovery phase tools and action status.
Create `aegis_demo.py` for recovery, resume and reproducible evaluation with JSON
evidence. Runtime config comes from environment for MCP; tools cannot provide an
arbitrary endpoint. Include real MCP-client smoke verification.

## Task 4: Dashboard and submission evidence

Add measured-recovery controls and before/after evidence to `aegis_dashboard.py`.
Relabel graph-only and policy-lineage comparisons. Update README with graph
schema, execution boundary, measured demo setup, failure/restart scenarios, AI
assistance disclosure and demo script. Do not invent a video/deployment URL.

## Task 5: Verification and independent review

Run full unit and live FalkorDB suites, HTTP/CLI/MCP smoke checks and dashboard
tests. Review final changes independently; fix substantive findings with failing
regression tests. Record commands/results in `docs/implementation-progress.md`.
Leave changes on `codex/verified-recovery` for review; do not merge or publish.
