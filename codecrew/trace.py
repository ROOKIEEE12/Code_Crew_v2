"""Append-only run tracing.

A multi-agent system cannot be debugged from print statements, and the
evaluation needs per-run token and latency figures that are impossible to
reconstruct afterwards. Every LLM call, agent step and gate outcome lands
here as one JSON object per line.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class Tracer:
    """Writes JSONL events for one run.

    Thread-safe because Phase 2 runs the Coder and Test-Author concurrently.
    """

    def __init__(self, path: Path, run_id: str | None = None) -> None:
        self.path = path
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        record = {
            "run_id": self.run_id,
            "t": round(time.monotonic() - self._started, 3),
            "kind": kind,
            **fields,
        }
        line = json.dumps(record, default=_fallback, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read(self) -> list[dict]:
        """Load the events back, for the run summary and the evaluation."""
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def totals(self) -> dict[str, Any]:
        """Aggregate token and latency cost across the run."""
        calls = [e for e in self.read() if e["kind"] == "llm_call"]
        prompt = sum(e.get("prompt_tokens") or 0 for e in calls)
        completion = sum(e.get("completion_tokens") or 0 for e in calls)
        return {
            "llm_calls": len(calls),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "llm_seconds": round(sum(e.get("latency_s") or 0.0 for e in calls), 2),
            "retries": sum(1 for e in self.read() if e["kind"] == "llm_retry"),
        }


class NullTracer(Tracer):
    """Discards events. Used in unit tests that do not assert on tracing."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips super().__init__
        self.path = Path(os.devnull)
        self.run_id = "null"
        self._lock = threading.Lock()
        self._started = time.monotonic()

    def event(self, kind: str, **fields: Any) -> None:
        return

    def read(self) -> list[dict]:
        return []


def _fallback(value: Any) -> str:
    return str(value)
