"""Task 3: the application services behind `cplane init` and home resolution."""

from pathlib import Path

import pytest

from control_plane.app.home import init_home, locate_home
from control_plane.store.git import Git
from control_plane.store.home import HomeExistsError, HomeNotFoundError


def test_init_home_creates_a_committed_home_at_the_path(tmp_path: Path) -> None:
    home = init_home(tmp_path / "home")

    assert home.plane_yaml.is_file()
    assert Git(home.path).status() == []


def test_init_home_refuses_an_existing_home(tmp_path: Path) -> None:
    init_home(tmp_path / "home")

    with pytest.raises(HomeExistsError):
        init_home(tmp_path / "home")


def test_locate_home_uses_option_then_environment(tmp_path: Path) -> None:
    home = init_home(tmp_path / "home")

    assert locate_home(str(home.path), {}) == home
    assert locate_home(None, {"CPLANE_HOME": str(home.path)}) == home
    with pytest.raises(HomeNotFoundError):
        locate_home(None, {})
