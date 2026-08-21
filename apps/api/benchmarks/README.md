# Phase 16D3 benchmark

This package is intentionally separate from `apps/api/tests`.  It records
wall time, SQL executions, reliable DBAPI row counts, ORM hydration, Python
peak allocation, route/fallback evidence, projection/audit fields, and scene
sequence continuity in stable JSON.

Run the fast smoke proof with:

```powershell
pytest apps/api/benchmarks/test_phase16d3_scale.py -q -s
```

Run the 10k and 100k isolated corpus proofs explicitly:

```powershell
$env:RUN_PHASE16D3 = "1"
pytest apps/api/benchmarks/test_phase16d3_scale.py -q -s
```

The large fixture is created and measured in separate phases.  Fixture
construction is not included in the bounded-read timings.  The benchmark does
not claim a fast path merely because a legacy result matches: the measured
operation records its route, SQL count, hydration count, and sequence proof.
PostgreSQL concurrency and full audit suites remain separate PG-only jobs and
must be run with `DATABASE_URL` set to PostgreSQL; SQLite is never used as a
concurrency substitute.

The opt-in suite also contains a real continuous SceneCommit certification for
10,000 and 100,000 scenes. Each scene creates a new proposal, performance and
turn on one Project and goes through the production commit service. Report
matrices remain fail-closed: an unexecuted route, audit, fault, or concurrency
case is `PENDING`, never an implied `PASS`.

Run the PostgreSQL append/rebuild certification only against the real PG
service:

```powershell
$env:RUN_PHASE16D3 = "1"
$env:DATABASE_URL = "postgresql+psycopg://..."
.venv\Scripts\python.exe -m pytest apps/api/benchmarks/test_phase16d3_postgres.py -q
```

It covers concurrent formal-state, history projection, narrative projection,
cognition, research, ledger rebuilds, plus append-vs-rebuild serialization.
