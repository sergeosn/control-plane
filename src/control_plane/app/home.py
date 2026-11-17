"""Application services for the home repository: `cplane init` and home resolution."""

from collections.abc import Mapping
from pathlib import Path

from control_plane.store.home import Home, create_home


def init_home(path: Path) -> Home:
    """Create a home repository with the `development` lane and the default rules."""
    return create_home(path)


def locate_home(option: str | None, environ: Mapping[str, str]) -> Home:
    """The home named by `--home`, else by `CPLANE_HOME`."""
    return Home.locate(option, environ)
