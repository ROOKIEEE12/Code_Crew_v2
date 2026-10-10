"""Benchmark: tasks with hidden tests, and the false-confidence metric."""

from .runner import (
    BUILD_FAILED,
    CAUGHT,
    FALSE_ALARM,
    FALSE_CONFIDENCE,
    TRUE_PASS,
    BenchRunner,
    BenchSummary,
    TaskOutcome,
    compare_results,
    render_summary,
    summarize,
    write_results,
)
from .tasks import TASKS, Task, get_tasks

__all__ = [
    "compare_results",
    "TASKS",
    "Task",
    "get_tasks",
    "BenchRunner",
    "BenchSummary",
    "TaskOutcome",
    "summarize",
    "render_summary",
    "write_results",
    "TRUE_PASS",
    "FALSE_CONFIDENCE",
    "CAUGHT",
    "FALSE_ALARM",
    "BUILD_FAILED",
]
