"""Task 1: the empty package has the layout from SPEC.md section 17 and the checks run."""

import asyncio
import importlib
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SUBPACKAGES = [
    "app",
    "domain",
    "store",
    "llm",
    "agent",
    "tools",
    "policy",
    "context",
    "workspace",
    "gates",
    "flow",
]


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_from_section_17_is_importable(name: str) -> None:
    module = importlib.import_module(f"control_plane.{name}")

    assert module.__name__ == f"control_plane.{name}"


def test_tests_cannot_open_network_connections() -> None:
    with pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)


async def test_async_tests_run_without_a_marker() -> None:
    await asyncio.sleep(0)


def test_ci_workflow_runs_lint_format_check_and_tests_with_coverage() -> None:
    workflows = list((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

    assert workflows, "no GitHub Actions workflow found"
    text = "\n".join(path.read_text() for path in workflows)
    assert "ruff check" in text
    assert "ruff format --check" in text
    assert "pytest --cov" in text
