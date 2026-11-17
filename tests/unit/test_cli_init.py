"""Task 3: `cplane init <path>` and the `--home` / `CPLANE_HOME` resolution."""

from pathlib import Path

import pytest

from control_plane.cli import build_parser, main, resolve_home
from control_plane.store.git import Git
from control_plane.store.home import HomeNotFoundError, create_home


def test_init_creates_a_home_repository_and_prints_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "plane-home"

    code = main(["init", str(target)])

    assert code == 0
    assert capsys.readouterr().out.strip() == f"Initialized control plane home at {target}"
    assert (target / "plane.yaml").is_file()
    assert (target / "lanes" / "development" / "flow.yaml").is_file()
    assert (target / "rules" / "tools.yaml").is_file()
    assert Git(target).log_subjects() == ["Initialize control plane home"]


def test_init_on_a_non_empty_directory_fails_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "junk.txt").write_text("")

    code = main(["init", str(tmp_path)])

    assert code == 1
    assert "not empty" in capsys.readouterr().err
    assert not (tmp_path / "plane.yaml").exists()


def test_home_option_is_accepted_before_the_command() -> None:
    args = build_parser().parse_args(["--home", "/x", "init", "/y"])

    assert args.home == "/x"
    assert args.command == "init"
    assert args.path == "/y"


def test_resolve_home_prefers_the_option_over_cplane_home(tmp_path: Path) -> None:
    by_option = create_home(tmp_path / "a")
    by_env = create_home(tmp_path / "b")
    args = build_parser().parse_args(["--home", str(by_option.path), "init", "/y"])

    assert resolve_home(args, {"CPLANE_HOME": str(by_env.path)}) == by_option
    assert (
        resolve_home(build_parser().parse_args(["init", "/y"]), {"CPLANE_HOME": str(by_env.path)})
        == by_env
    )


def test_resolve_home_without_either_raises(tmp_path: Path) -> None:
    with pytest.raises(HomeNotFoundError):
        resolve_home(build_parser().parse_args(["init", "/y"]), {})
