# Verified recovery progress

Plan: `docs/superpowers/plans/2026-10-18-verified-recovery.md`

- Ruling: the user's explicit request to start building authorizes the reviewed
  roadmap and inline execution. Preserve the agreed scope in the spec and plan;
  do not introduce another permission round for routine implementation choices.
- Ruling: use the fresh, empty task checkout on a `codex/` branch. There are no
  existing files or local changes requiring a second worktree.
- Baseline: dependencies initially unavailable; installing in `.venv`. Docker
  is installed but its daemon was initially offline. No product test failures
  have been established.
- Preflight: runtime receipts are consumed by coordinator execution; coordinator
  signatures are consumed by CLI, MCP and dashboard. Preserve these interfaces.
- Baseline: 78 passed, 54 live tests skipped (existing suite before changes).
- Task 1/2: 17 HTTP and live recovery tests passed. RED evidence: missing runtime
  and coordinator modules; then four live failures at the missing observed-
  latency commit interface. GREEN: 17 passed after implementing that interface.
- Ruling: legacy graph-only commits remain available with `model_projection`
  evidence labels. New measured recovery rewards only after HTTP verification.
  Existing synthetic-demo tests retain their documented semantics.
- Task 3: actual stdio MCP client passed both live tests, including an approved
  handoff resumed by a second MCP server process. The controller is explicitly
  bounded, not claimed to be an independently reasoning LLM.
- Task 4: dashboard interaction test passed with measured recovery evidence.
- Evaluation: 8/8 scenarios passed in the first actual run; repeated-fixture
  memory comparison used 1 attempt with memory and 2 without it. Results reside
  in `scratch/evaluation.json` and must be regenerated after substantive changes.
- Additional RED/GREEN checks: runtime identity binding; preserving modeled vs
  observed latency fields; viewing lineage without training memory; concurrent
  duplicate workers; policy change after an interrupted HTTP effect.
- Ruling: a lost action compare-and-set raises RecoveryBusy without compensation,
  allowing the worker with the current version to finish. Runtime receipts are
  inspected before compensating interrupted work. HTTP and FalkorDB remain a
  documented distributed-transaction boundary; there is no claim of atomicity
  across those systems.
- Full live verification exposed 9 failures: unsupported audit regex syntax,
  learning journal/receipt values reading pre-update properties, and legacy route
  load release reading an absent pre-update contribution. The existing regression
  tests supplied RED evidence. Fix: supported string.matchRegEx; precompute next
  weights, sequences and old load contributions and use them consistently.
- Targeted recheck: 10 live cases passed; one UI test still asserted the removed
  unsupported query spelling. Ruling: assert the UI displays the exact executed
  Cypher instead, while retaining the real evidence-row assertions.
- Independent review identified one Important stale-failure-worker race. Reproduced
  it deterministically: a worker paused before compensation disabled a route after
  another worker had committed VERIFIED. Added STOPPING/FINALIZING version claims
  and a recovery action-version guard inside the atomic success commit.
- Final review declined production cloud/auth/LLM requirements and distributed
  transactions as out of scope. Ruling: retain the documented local simulator and
  HTTP/graph boundary; fix coordinated-worker races within that boundary.
- Review-fix verification: 15 recovery tests passed, including stale-worker
  compensation and resume after success commit. No deferred review findings.
- Repeated-incident regression exposed a floating-point round-trip mismatch in
  raw Cypher snapshot parameters. Database-side canonical JSON alone also exposed
  differing property-map iteration order between read and write query plans.
  Ruling: use sorted key/value property lists and the database's own serialized
  numeric representation for freshness, keeping full raw evidence alongside it.
  This preserves all operational fields without rounding or weakening the guard.
- Targeted canonical check: 8 passed, including repeated observed recovery,
  stale-worker compensation and all existing changed-operational-input cases.
- Final verification: `AEGIS_TEST_FALKORDB=1 .venv/Scripts/python.exe -m pytest -q`
  -> 161 passed, no skips, in 296.92 seconds. Evaluation then ran serially:
  `python aegis_demo.py --evaluate --output scratch/evaluation-release.json`
  -> 8/8 passed; learned fixture used 1 attempt versus 2 baseline attempts.
- Dependency check: `python -m pip check` -> no broken requirements.
- Browser check: localhost:8501 completed measured MCP recovery. US rejected,
  EU VERIFIED; displayed measured HTTP p95 394.8 ms -> 114.7 ms, separate from
  modeled dependency-path latency 140 ms. Screenshot: scratch/measured-recovery.jpg.
- All five plan tasks complete. No deferred independent-review findings. Work
  stays on codex/verified-recovery; no merge, push, public deployment or external
  notification was performed. A submission video remains a human deliverable.
- Ruling: Git had no author identity configured. Use the explicit local author
  `Codex <codex@local.invalid>` for this commit only, without changing user/global
  configuration or attributing assistant edits to a guessed human identity.
