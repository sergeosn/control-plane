"""Run metrics computed directly from run folders (docs/SPEC.md section 16)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from control_plane.domain import Approval, Event, LLMCall, ToolCall
from control_plane.domain.gates import GateReport
from control_plane.flow.state import RunResult
from control_plane.store.files import read_jsonl
from control_plane.store.home import Home
from control_plane.store.runs import (
    APPROVALS_FILE,
    EVENTS_FILE,
    LLM_CALLS_FILE,
    RESULT_FILE,
    STEPS_DIR,
    TOOL_CALLS_FILE,
    RunStore,
)


def _json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _median(values: list[int | float]) -> int | float | None:
    return median(values) if values else None


def _records(path: Path, model: type[Any]) -> list[Any]:
    """Read an audit stream, ignoring a damaged artifact rather than aborting the report."""
    try:
        return read_jsonl(path, model)
    except (OSError, ValueError):
        return []


def run_report(home: Home) -> dict[str, Any]:
    """Return the stable, JSON-serialisable report for all folders under ``home/runs``.

    Cost and audit counts include unfinished folders: they describe work already performed.
    Iterations and wall time are only meaningful for finished folders and therefore use the
    result/event artifacts of finished runs.
    """
    finished_iterations: list[int] = []
    finished_wall: list[float] = []
    total_cost = 0.0
    merged = 0
    total = 0
    gate_failures: Counter[str] = Counter()
    tool_calls: defaultdict[str, Counter[str]] = defaultdict(Counter)

    for run_id in RunStore(home).list_ids():
        folder = home.runs / run_id
        total += 1
        result_data = _json(folder / RESULT_FILE)
        try:
            result = RunResult.model_validate(result_data) if result_data else None
        except ValueError:
            result = None
        if result is not None:
            finished_iterations.append(result.iterations)
            approvals = _records(folder / APPROVALS_FILE, Approval)
            if result.state == "done" and not any(a.type == "question" for a in approvals):
                merged += 1
            events = _records(folder / EVENTS_FILE, Event)
            if len(events) >= 2:
                times = [event.ts for event in events]
                finished_wall.append((max(times) - min(times)).total_seconds() / 60)

        steps = folder / STEPS_DIR
        if steps.is_dir():
            for step in sorted(steps.iterdir()):
                calls = _records(step / LLM_CALLS_FILE, LLMCall)
                total_cost += sum(call.cost_usd for call in calls)
                for call in _records(step / TOOL_CALLS_FILE, ToolCall):
                    class_name = call.tool_class or "unclassified"
                    tool_calls[class_name][call.decision] += 1

        gates = folder / "gates"
        if gates.is_dir():
            for attempt in sorted(gates.iterdir()):
                report = _json(attempt / "results.json")
                if report is None:
                    continue
                try:
                    parsed = GateReport.model_validate(report)
                except ValueError:
                    continue
                for name in parsed.failed:
                    gate_failures[name] += 1

    return {
        "runs": {
            "total": total,
            "merged_without_human_input": merged,
        },
        "iterations": {"median": _median(finished_iterations)},
        "cost_usd": {
            "median": _median([total_cost_for_run(home, run_id) for run_id in _finished_ids(home)]),
            "total": total_cost,
        },
        "wall_minutes": {"median": _median(finished_wall)},
        "gate_failures": {name: gate_failures[name] for name in sorted(gate_failures)},
        "tool_calls": {
            name: {decision: counts[decision] for decision in sorted(counts)}
            for name, counts in sorted(tool_calls.items())
        },
    }


def _finished_ids(home: Home) -> list[str]:
    finished: list[str] = []
    for run_id in RunStore(home).list_ids():
        data = _json(home.runs / run_id / RESULT_FILE)
        try:
            if data:
                RunResult.model_validate(data)
                finished.append(run_id)
        except ValueError:
            continue
    return finished


def total_cost_for_run(home: Home, run_id: str) -> float:
    total = 0.0
    steps = home.runs / run_id / STEPS_DIR
    if steps.is_dir():
        for step in steps.iterdir():
            total += sum(call.cost_usd for call in _records(step / LLM_CALLS_FILE, LLMCall))
    return total
