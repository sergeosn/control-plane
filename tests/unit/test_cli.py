"""Task 1: `cplane --version` prints the version."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from control_plane import __version__
from control_plane.cli import main

VERSION_LINE = re.compile(r"^cplane \d+\.\d+\.\d+\S*$")


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    out = capsys.readouterr().out.strip()
    assert VERSION_LINE.match(out), out
    assert out == f"cplane {__version__}"


def test_installed_cplane_script_prints_version() -> None:
    script = Path(sys.executable).parent / "cplane"
    assert script.exists(), f"{script} is not installed; run `uv sync`"

    result = subprocess.run([str(script), "--version"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"cplane {__version__}"


def test_no_arguments_prints_usage_and_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main([])

    assert code != 0
    assert "usage: cplane" in capsys.readouterr().err
