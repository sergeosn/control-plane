"""On-disk coverage for run-folder metrics."""

import json
from pathlib import Path

from control_plane.app.report import run_report
from control_plane.store.home import create_home
from control_plane.store.runs import RunStore


def _line(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")


def _run(
    home,
    run_id: str,
    *,
    state: str | None,
    iterations: int = 0,
    cost: float = 0.0,
    start: str = "2026-01-01T00:00:00Z",
    end: str = "2026-01-01T00:10:00Z",
    question: bool = False,
) -> None:
    run = RunStore(home).create(run_id)
    if state is not None:
        (run.path / "result.json").write_text(
            json.dumps({"state": state, "reason": None, "iterations": iterations})
        )
    _line(run.path / "events.jsonl", {"ts": start, "run_id": run_id, "kind": "started"})
    _line(run.path / "events.jsonl", {"ts": end, "run_id": run_id, "kind": "ended"})
    if question:
        _line(
            run.path / "approvals.jsonl",
            {
                "id": "q-1",
                "run_id": run_id,
                "type": "question",
                "asked": "why?",
                "evidence": [],
                "created_at": start,
                "answer": "because",
                "decided_at": end,
            },
        )
    if cost:
        _line(
            run.path / "steps/01-plan-1/llm_calls.jsonl",
            {
                "ts": start,
                "model": "m",
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cost_usd": cost,
                "stop_reason": "end",
            },
        )


def test_report_reads_finished_and_unfinished_run_folders(tmp_path: Path) -> None:
    home = create_home(tmp_path / "home")
    _run(home, "R-1", state="done", iterations=2, cost=1.0)
    _run(home, "R-2", state="failed", iterations=4, cost=3.0, question=True)
    _run(home, "R-3", state=None, cost=5.0)

    folder = home.runs / "R-1" / "gates/1"
    folder.mkdir(parents=True)
    (folder / "results.json").write_text(
        json.dumps(
            {
                "attempt": 1,
                "outcome": "fail",
                "out_of_plan": [],
                "gates": [
                    {"name": "lint", "status": "fail", "summary": "x", "output_file": "x"},
                    {"name": "tests", "status": "fail", "summary": "x", "output_file": "x"},
                ],
            }
        )
    )
    _line(
        home.runs / "R-1/steps/01-plan-1/tool_calls.jsonl",
        {
            "ts": "2026-01-01T00:01:00Z",
            "tool_use_id": "a",
            "tool": "read_file",
            "arguments": {},
            "tool_class": "read",
            "decision": "allow",
            "rule_id": "r",
        },
    )
    _line(
        home.runs / "R-1/steps/01-plan-1/tool_calls.jsonl",
        {
            "ts": "2026-01-01T00:02:00Z",
            "tool_use_id": "b",
            "tool": "x",
            "arguments": {},
            "tool_class": None,
            "decision": "deny",
            "rule_id": None,
        },
    )

    assert run_report(home) == {
        "runs": {"total": 3, "merged_without_human_input": 1},
        "iterations": {"median": 3},
        "cost_usd": {"median": 2.0, "total": 9.0},
        "wall_minutes": {"median": 10.0},
        "gate_failures": {"lint": 1, "tests": 1},
        "tool_calls": {"read": {"allow": 1}, "unclassified": {"deny": 1}},
    }


def test_report_empty_and_malformed_artifacts_are_safe(tmp_path: Path) -> None:
    home = create_home(tmp_path / "home")
    run = RunStore(home).create("R-1")
    (run.path / "result.json").write_text("not json")
    (run.path / "steps/01/llm_calls.jsonl").parent.mkdir(parents=True)
    (run.path / "steps/01/llm_calls.jsonl").write_text("bad\n")
    assert run_report(home) == {
        "runs": {"total": 1, "merged_without_human_input": 0},
        "iterations": {"median": None},
        "cost_usd": {"median": None, "total": 0.0},
        "wall_minutes": {"median": None},
        "gate_failures": {},
        "tool_calls": {},
    }
