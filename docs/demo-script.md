# Four-minute Aegis demonstration

Use a clean demo graph and close unrelated windows. Record actual outcomes; if a
check fails, show the failure and its evidence. Do not substitute projected
latency for an observation. All services and company data are synthetic.

## 0:00–0:30 — Establish the incident

"A payment route is slow. The fastest alternative conflicts with a newer company
policy. Aegis must find a permitted route and prove that its action worked."

Start measured services in the dashboard. Show the active primary gateway and
the failed latency baseline. Explain that these are real local HTTP requests.

## 0:30–1:30 — Explain the graph's decision

Run measured recovery. Show the tool trace and the rejected US candidate.
Follow `Decision-SUPERSEDES-Decision` and `Decision-GOVERNS-Pipeline` evidence.
Show the EU shadow approval, then the router receipt and post-action samples.
Point out that predicted dependency-path latency and measured gateway p95 are
different quantities. Download the JSON evidence.

## 1:30–2:30 — Show independent phases and a changing policy

Reset the graph and local incident. Prepare a handoff and show its APPROVED action
ID. Change a regional policy in the sidebar before executing. Resume the handoff:
it must become STALE without a route change. Explain why a previous approval
cannot authorize a new policy context.

## 2:30–3:15 — Show a restart

Use the terminal MCP demo from the README. Run the planner/auditor command with
`--stop-after-approval`; run a separate command/process with `--resume` and the
returned action ID. Repeat the resume command to show the same receipt with no
additional routing effect or success reward. Read the persistent action journal.

## 3:15–4:00 — Present measured evaluation

Open the generated `scratch/evaluation.json` from a fresh `--evaluate` run.
Show all eight case outcomes, including failure and all-blocked cases. Show the
memory comparison's candidate-attempt counts. Explain the small fixture and
three-sample limits; make no claims about unseen incidents, real financial
penalties avoided, or general vector-RAG superiority.

Close with: "FalkorDB supplies current constraints, shared action state and
experience. The agent's route choice, authorization and next attempt depend on
that graph; measured HTTP observations establish what actually happened."
