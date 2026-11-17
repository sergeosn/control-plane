"""Fixtures shared by the store tests: a fresh home repository per test."""

from pathlib import Path

import pytest

from control_plane.store.home import Home, create_home


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return create_home(tmp_path / "home")
