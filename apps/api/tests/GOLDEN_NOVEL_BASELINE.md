# Golden Novel Baseline

`golden_novel_fixture.py` is the first deterministic long-form fiction corpus.
It intentionally contains no real model output and no database state. The
corpus is a contract for regression tests, not a finished novel.

Run the phase-0 checks with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q apps/api/tests/test_golden_novel_baseline.py
```

The tests cover:

- 10 characters, 25 canon facts, 10 threads, 3 arcs, and 30 chapters;
- 20 chronological events and location-overlap detection;
- character knowledge leaks and explicit false beliefs;
- premature reveals and invalid foreshadowing status;
- required and forbidden chapter events;
- deterministic anti-AI expression checks shared with the quality gate.

When the corpus is expanded, keep IDs stable. Add deliberate failures to the
tests before adding a new detector; the detector must fail the injected case
and pass the clean corpus.
