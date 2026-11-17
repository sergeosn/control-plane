"""The `cplane` command line.

Every command calls the application layer in `control_plane.app` and never writes files or
the index itself (SPEC.md section 14). Every command other than `init` finds the home
repository through `--home` or `CPLANE_HOME` (`resolve_home`).
"""

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.app import home as app_home
from control_plane.store.git import GitError
from control_plane.store.home import ENV_VAR, Home, HomeExistsError, HomeNotFoundError

CommandError = (HomeExistsError, HomeNotFoundError, GitError)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cplane", description="Control Plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help=f"path of the home repository (default: ${ENV_VAR})")
    commands = parser.add_subparsers(dest="command")

    init = commands.add_parser(
        "init", help="create a home repository with the development lane and default rules"
    )
    init.add_argument("path", help="where to create the home repository")
    return parser


def resolve_home(args: argparse.Namespace, environ: Mapping[str, str] | None = None) -> Home:
    """The home a command works on, from `--home` or `CPLANE_HOME`."""
    return app_home.locate_home(args.home, os.environ if environ is None else environ)


def _init(args: argparse.Namespace) -> int:
    home = app_home.init_home(Path(args.path))
    print(f"Initialized control plane home at {home.path}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {"init": _init}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        return COMMANDS[args.command](args)
    except CommandError as error:
        print(f"cplane: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
