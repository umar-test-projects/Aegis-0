# Verified recovery evaluation — 18 October 2026

The final local run passed all **161 tests**, with live FalkorDB integration
enabled and no skipped tests:

```powershell
$env:AEGIS_TEST_FALKORDB = '1'
.\.venv\Scripts\python.exe -m pytest -q
```

The independently runnable evaluation passed **8/8 disclosed synthetic cases**:

| Case | Observed state | Passed |
| --- | --- | --- |
| Recovery after policy rejection of the US route | VERIFIED | Yes |
| Policy changed after approval | STALE | Yes |
| Every candidate blocked | BLOCKED | Yes |
| Approved gateway fails actual requests | FAILED | Yes |
| Executor stops after applying HTTP effect, then resumes | VERIFIED | Yes |
| Completed action executed again | VERIFIED, original receipt | Yes |
| Policy changes during HTTP execution | STALE, router disabled | Yes |
| Memory enabled/disabled on a repeated fixture | VERIFIED | Yes |

On the repeated fixture, learned recall needed **1 candidate attempt**, compared
with **2 attempts** when ranking used the original baselines. This is a small
fixture comparison of attempt counts, not a general recovery-time benchmark.

```powershell
.\.venv\Scripts\python.exe aegis_demo.py --evaluate --output scratch/evaluation.json
```

Each evaluation uses disposable `aegis_test_*` graphs. Complete action, shadow
audit, receipt and HTTP sample evidence is exported before those graphs are
deleted. The release run's complete local artifact is
[evaluation-release.json](evidence/evaluation-release.json); reproduce it with the command above.

The test suite also covers two independent MCP processes handing off an action,
dashboard interactions, stale-runtime identity, concurrent workers, compensation
fencing, canonical snapshots containing measured floats, and the existing
Company Brain/Slack/reporting regressions.

Observations are real loopback router/gateway requests to synthetic services.
Dependency-path latency remains a graph model. Three request samples per recovery
demonstrate the closed loop; they do not establish a production percentile SLA.
