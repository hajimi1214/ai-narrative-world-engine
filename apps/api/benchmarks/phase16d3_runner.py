"""Independent Phase 16D3 benchmark instrumentation.

This module deliberately has no production dependencies beyond SQLAlchemy.  It
measures a supplied callable and records enough evidence to distinguish a
bounded read from a Python-side hydration or fallback.  The runner does not
change formal rows and is safe to use with an isolated benchmark database.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import time
import tracemalloc
from typing import Any, Callable

from sqlalchemy import event
from sqlalchemy.orm import Session


@dataclass
class BenchmarkMetrics:
    name: str
    scale: int
    wall_time_ms: float = 0.0
    sql_query_count: int = 0
    sql_rows_returned: int = 0
    orm_object_hydration_count: int = 0
    python_peak_bytes: int = 0
    route: str | None = None
    fallback_reason: str | None = None
    projection_status: str | None = None
    audit_valid: bool | None = None
    formal_fingerprint: str | None = None
    derived_fingerprint: str | None = None
    scene_sequence_continuous: bool | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SQLAndHydrationProbe:
    """Capture SQL executions and ORM instances for one measured operation."""

    def __init__(self, session: Session):
        self.session = session
        self.engine = session.get_bind()
        self.sql_query_count = 0
        self.sql_rows_returned = 0
        self.orm_object_hydration_count = 0
        self._listening = False

    def _sql(self, _conn, cursor, _statement, _parameters, _context, _executemany):
        self.sql_query_count += 1
        # DBAPIs commonly report -1 for SELECT rowcount.  Keep only reliable
        # non-negative values; query count remains the portable bound.
        if cursor.rowcount is not None and cursor.rowcount >= 0:
            self.sql_rows_returned += cursor.rowcount

    def _hydrate(self, _session, instance):
        self.orm_object_hydration_count += 1

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._sql)
        event.listen(self.session, "loaded_as_persistent", self._hydrate)
        self._listening = True
        return self

    def __exit__(self, *_exc):
        if self._listening:
            event.remove(self.engine, "before_cursor_execute", self._sql)
            event.remove(self.session, "loaded_as_persistent", self._hydrate)
            self._listening = False


def measure(
    session: Session,
    *,
    name: str,
    scale: int,
    operation: Callable[[], Any],
    route: str | None = None,
    fallback_reason: str | None = None,
    projection_status: str | None = None,
    audit_valid: bool | None = None,
    formal_fingerprint: str | None = None,
    derived_fingerprint: str | None = None,
    scene_sequence_continuous: bool | None = None,
    details: dict[str, Any] | None = None,
) -> BenchmarkMetrics:
    """Measure one operation without mutating its result or formal payload."""
    probe = SQLAndHydrationProbe(session)
    tracemalloc.start()
    started = time.perf_counter()
    with probe:
        operation()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return BenchmarkMetrics(
        name=name,
        scale=scale,
        wall_time_ms=round(elapsed * 1000, 3),
        sql_query_count=probe.sql_query_count,
        sql_rows_returned=probe.sql_rows_returned,
        orm_object_hydration_count=probe.orm_object_hydration_count,
        python_peak_bytes=peak,
        route=route,
        fallback_reason=fallback_reason,
        projection_status=projection_status,
        audit_valid=audit_valid,
        formal_fingerprint=formal_fingerprint,
        derived_fingerprint=derived_fingerprint,
        scene_sequence_continuous=scene_sequence_continuous,
        details=details or {},
    )


def scene_sequence_is_continuous(sequences: list[int]) -> bool:
    return sequences == list(range(1, len(sequences) + 1))


def report_json(metrics: list[BenchmarkMetrics]) -> str:
    """Stable machine-readable output for CI/artifact upload."""
    return json.dumps([item.as_dict() for item in metrics], ensure_ascii=False, sort_keys=True, indent=2)
