"""CLI coverage for the run report."""

import json
from pathlib import Path

from control_plane.cli import main
from control_plane.store.home import create_home
from control_plane.store.runs import RunStore


def test_report_text_and_json_are_available(tmp_path: Path, capsys) -> None:
    home = create_home(tmp_path / "home")
    run = RunStore(home).create("R-1")
    (run.path / "result.json").write_text(
        json.dumps({"state": "done", "reason": None, "iterations": 2})
    )
    code = main(["--home", str(home.path), "report"])
    assert code == 0
    text = capsys.readouterr().out
    assert "Runs merged with no human input other than approvals: 1" in text
    assert "Median iterations: 2" in text
    assert "Median cost:" in text and "Total cost:" in text
    assert "Median wall time:" in text
    assert "Gate failures:" in text and "Tool calls:" in text

    assert main(["--home", str(home.path), "report", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"] == {"total": 1, "merged_without_human_input": 1}
    assert set(payload) == {
        "runs",
        "iterations",
        "cost_usd",
        "wall_minutes",
        "gate_failures",
        "tool_calls",
    }
