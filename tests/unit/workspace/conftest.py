"""Fixtures for the workspace tests: a project repository and a worktrees folder."""

from pathlib import Path

import pytest

from control_plane.store.git import Git
from fixtures.project_repo import make_project


@pytest.fixture
def project(tmp_path: Path) -> Git:
    return make_project(tmp_path / "project")


@pytest.fixture
def worktrees(tmp_path: Path) -> Path:
    """Where run worktrees go: `<home>/.worktrees` in production."""
    return tmp_path / "home" / ".worktrees"
