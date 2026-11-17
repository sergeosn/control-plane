"""The home repository: the git repository that holds everything durable (SPEC.md section 5).

`create_home` lays out a new home from the template shipped with the package and makes its
first commit. `Home.locate` is how every command finds an existing home.
"""

import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from control_plane.store.git import Git

ENV_VAR = "CPLANE_HOME"
HOME_MARKER = "plane.yaml"
INITIAL_COMMIT = "Initialize control plane home"
TEMPLATE_DIR = Path(__file__).parent / "home_template"
GITIGNORE = ".index/\n.worktrees/\n"
TRACKED_EMPTY_DIRS = ("projects", "tasks", "runs")


class HomeNotFoundError(Exception):
    """No home was given, or the path given is not a home."""


class HomeExistsError(Exception):
    """`create_home` was pointed at a directory that already holds something."""


@dataclass(frozen=True)
class Home:
    path: Path

    @property
    def plane_yaml(self) -> Path:
        return self.path / HOME_MARKER

    @property
    def projects(self) -> Path:
        return self.path / "projects"

    @property
    def lanes(self) -> Path:
        return self.path / "lanes"

    @property
    def rules(self) -> Path:
        return self.path / "rules"

    @property
    def tasks(self) -> Path:
        return self.path / "tasks"

    @property
    def runs(self) -> Path:
        return self.path / "runs"

    @property
    def index(self) -> Path:
        return self.path / ".index"

    @property
    def worktrees(self) -> Path:
        return self.path / ".worktrees"

    @classmethod
    def locate(cls, option: str | None, environ: Mapping[str, str]) -> "Home":
        """The home named by the `--home` option, else by `CPLANE_HOME`."""
        raw = option or environ.get(ENV_VAR)
        if not raw:
            raise HomeNotFoundError(f"no home repository: pass --home or {ENV_VAR}")
        path = Path(raw).expanduser().resolve()
        if not (path / HOME_MARKER).is_file():
            raise HomeNotFoundError(f"{path} is not a home repository: no {HOME_MARKER} in it")
        return cls(path)


def create_home(path: Path) -> Home:
    """Create the section 5 layout at `path`, as a git repository with its first commit."""
    path = path.expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        raise HomeExistsError(f"{path} exists and is not empty")
    path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_DIR, path, dirs_exist_ok=True)
    (path / ".gitignore").write_text(GITIGNORE)
    for name in TRACKED_EMPTY_DIRS:
        (path / name).mkdir()
        (path / name / ".gitkeep").write_text("")
    (path / ".index").mkdir()
    git = Git(path)
    git.init()
    git.add(".")
    git.commit(INITIAL_COMMIT)
    return Home(path)
