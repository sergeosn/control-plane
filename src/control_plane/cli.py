"""The `cplane` command line.

Every command calls the application layer in `control_plane.app` and never writes files or
the index itself (SPEC.md section 14). Every command other than `init` finds the home
repository through `--home` or `CPLANE_HOME` (`resolve_home`).
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.app import home as app_home
from control_plane.app import index as app_index
from control_plane.store.git import GitError
from control_plane.store.home import ENV_VAR, Home, HomeExistsError, HomeNotFoundError
from control_plane.store.index import DB_FILE

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

    commands.add_parser("reindex", help="rebuild the index and verify finished run folders")
    return parser


def resolve_home(args: argparse.Namespace, environ: Mapping[str, str] | None = None) -> Home:
    """The home a command works on, from `--home` or `CPLANE_HOME`."""
    return app_home.locate_home(args.home, os.environ if environ is None else environ)


def _init(args: argparse.Namespace) -> int:
    home = app_home.init_home(Path(args.path))
    print(f"Initialized control plane home at {home.path}")
    return 0


def _reindex(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    report = asyncio.run(app_index.reindex(home))
    print(f"Rebuilt index at {home.index / DB_FILE}")
    print(", ".join(f"{table}: {count}" for table, count in report.counts.items()))
    for problem in report.problems:
        print(f"cplane: not indexed: {problem}", file=sys.stderr)
    if report.discrepancies:
        for found in report.discrepancies:
            detail = f"{found.kind} {found.path}".strip()
            print(
                f"cplane: finished run {found.run_id} differs from its commit: {detail}",
                file=sys.stderr,
            )
    else:
        print("Finished run folders match their commits.")
    return 0 if report.ok else 1


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {"init": _init, "reindex": _reindex}


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
