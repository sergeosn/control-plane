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
from control_plane.app import approvals as app_approvals
from control_plane.app import home as app_home
from control_plane.app import index as app_index
from control_plane.app import inspect as app_inspect
from control_plane.app.approvals import ApprovalError
from control_plane.app.inspect import InspectError
from control_plane.domain import Approval
from control_plane.store.git import GitError
from control_plane.store.home import ENV_VAR, Home, HomeExistsError, HomeNotFoundError
from control_plane.store.index import DB_FILE
from control_plane.store.runs import RunFinishedError

CommandError = (
    HomeExistsError,
    HomeNotFoundError,
    GitError,
    ApprovalError,
    RunFinishedError,
    InspectError,
)


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

    approvals = commands.add_parser("approvals", help="list the approval queue")
    approvals.add_argument("--all", action="store_true", help="include decided entries")

    approve = commands.add_parser("approve", help="approve an entry of the queue")
    approve.add_argument("id", help="the approval id, as `cplane approvals` lists it")
    approve.add_argument("--note", help="an optional note kept with the decision")

    deny = commands.add_parser("deny", help="deny an entry of the queue")
    deny.add_argument("id", help="the approval id, as `cplane approvals` lists it")
    deny.add_argument("--reason", required=True, help="why; the model sees this as its error")

    inspect = commands.add_parser("inspect", help="show what happened in a run and why")
    inspect.add_argument("run_id", help="the run to inspect")
    inspect.add_argument("--step", help="one step folder (01-plan-1) or step name (plan)")
    inspect.add_argument(
        "--context",
        action="store_true",
        help="per step: files provided, files read, tokens by source",
    )
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


def _approval_line(approval: Approval) -> str:
    line = (
        f"{approval.id}  {approval.run_id}  {approval.type}  "
        f"{approval.created_at.isoformat()}  {approval.asked}"
    )
    if approval.decided:
        line += f"  [{approval.answer}" + (f": {approval.note}]" if approval.note else "]")
    return line


def _approvals(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    queue = app_approvals.list_queue(home, include_decided=args.all)
    if not queue:
        print("No approvals." if args.all else "No pending approvals.")
        return 0
    for approval in queue:
        print(_approval_line(approval))
    return 0


def _approve(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    decided = asyncio.run(app_approvals.approve(home, args.id, note=args.note))
    print(f"Approved {decided.id} (run {decided.run_id}).")
    return 0


def _deny(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    decided = asyncio.run(app_approvals.deny(home, args.id, reason=args.reason))
    print(f"Denied {decided.id} (run {decided.run_id}): {decided.note}")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    if not args.context:
        print(f"Run {args.run_id}: steps")
        for folder in app_inspect.list_steps(home, args.run_id):
            print(f"  {folder}")
        print("Pass --context for the files each step was given and read.")
        return 0
    print(f"Run {args.run_id}: context per step")
    for report in app_inspect.inspect_context(home, args.run_id, args.step):
        print(
            f"Step {report.step}: {len(report.provided)} files provided, {report.read_count} read"
        )
        for file in report.provided:
            print(f"  {file.mark:<7} {file.path} ({file.lines} lines)")
        if report.read_elsewhere:
            print(f"  read outside the context: {', '.join(report.read_elsewhere)}")
        if report.tokens_by_source:
            sources = ", ".join(f"{k} {v}" for k, v in report.tokens_by_source.items())
            print(f"  tokens by source (estimated): {sources}")
        if report.measured_input_tokens is not None:
            print(f"  measured input tokens: {report.measured_input_tokens}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _init,
    "reindex": _reindex,
    "approvals": _approvals,
    "approve": _approve,
    "deny": _deny,
    "inspect": _inspect,
}


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
