"""The `cplane` command line.

Every command calls the application layer in `control_plane.app` and never writes files or
the index itself (docs/SPEC.md section 14). Every command other than `init` and `selfcheck`
finds the home repository through `--home` or `CPLANE_HOME` (`resolve_home`). `run` and
`resume` get their model clients from `make_clients`, with credentials from the environment.
"""

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from control_plane import __version__
from control_plane.agent import REASON_AUTH, REASON_CONNECTION, REASON_SERVER_ERROR
from control_plane.app import approvals as app_approvals
from control_plane.app import home as app_home
from control_plane.app import index as app_index
from control_plane.app import inspect as app_inspect
from control_plane.app import projects as app_projects
from control_plane.app import report as app_report
from control_plane.app import runs as app_runs
from control_plane.app import selfcheck as app_selfcheck
from control_plane.app import selfupdate as app_selfupdate
from control_plane.app import status as app_status
from control_plane.app import tasks as app_tasks
from control_plane.app.approvals import ApprovalError
from control_plane.app.clients import ClientFactory, RunError, environment_clients
from control_plane.app.inspect import InspectError
from control_plane.app.projects import ProjectError
from control_plane.app.runs import RunOutcome
from control_plane.app.selfupdate import SelfUpdateError
from control_plane.app.status import StatusError
from control_plane.app.tasks import TaskError
from control_plane.domain import Approval, ApprovalType
from control_plane.flow import FlowError
from control_plane.llm import CredentialsError
from control_plane.llm.openai_compat import AUTH_TOKEN_VAR
from control_plane.llm.types import Message
from control_plane.store.git import GitError
from control_plane.store.home import ENV_VAR, Home, HomeExistsError, HomeNotFoundError
from control_plane.store.index import DB_FILE
from control_plane.store.runs import RunFinishedError
from control_plane.workspace import WorkspaceError

CommandError = (
    HomeExistsError,
    HomeNotFoundError,
    GitError,
    ApprovalError,
    RunFinishedError,
    InspectError,
    ProjectError,
    TaskError,
    RunError,
    StatusError,
    CredentialsError,
    FlowError,
    WorkspaceError,
    SelfUpdateError,
)
USAGE_ERROR = 2


def make_clients(environ: Mapping[str, str]) -> ClientFactory:
    """The model clients of `run` and `resume`: real ones, credentials from the environment."""
    return environment_clients(environ)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cplane", description="Control Plane")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--home", help=f"path of the home repository (default: ${ENV_VAR})")
    commands = parser.add_subparsers(dest="command")

    init = commands.add_parser(
        "init", help="create a home repository with the development lane and default rules"
    )
    init.add_argument("path", help="where to create the home repository")

    project = commands.add_parser("project", help="manage projects").add_subparsers(
        dest="project_command", required=True
    )
    project_add = project.add_parser("add", help="register a project repository")
    project_add.add_argument("name", help="the project's name, a plain folder name")
    project_add.add_argument("--repo", required=True, help="the primary checkout, on main")
    project_add.add_argument(
        "--protected", action="append", default=[], help="a protected path (repeatable)"
    )

    task = commands.add_parser("task", help="manage task files").add_subparsers(
        dest="task_command", required=True
    )
    task_new = task.add_parser("new", help="write a new task in the backlog")
    task_new.add_argument("--file", help="add a task file written by hand, as it is")
    task_new.add_argument("--title")
    task_new.add_argument("--project")
    task_new.add_argument("--goal")
    task_new.add_argument(
        "--criterion", action="append", default=[], help="an acceptance criterion (repeatable)"
    )
    task_new.add_argument("--priority", type=int, default=2)
    task_new.add_argument("--plan-approval", action="store_true")
    task_new.add_argument("--lane")
    task_new.add_argument("--max-iterations", type=int)
    task_new.add_argument("--max-cost", type=float, help="max cost of a run in USD")
    task.add_parser("list", help="list the tasks")
    task_show = task.add_parser("show", help="print a task file")
    task_show.add_argument("id")

    run = commands.add_parser("run", help="run a task until the run finishes or parks")
    run.add_argument("task_id")

    resume = commands.add_parser("resume", help="continue parked runs whose approvals are decided")
    resume.add_argument("run_id", nargs="?")
    resume.add_argument("--all", action="store_true", help="every unfinished run, oldest first")

    status = commands.add_parser("status", help="state, current step, iteration and cost")
    status.add_argument("run_id", nargs="?")
    status.add_argument(
        "--json", action="store_true", help="print one machine-readable JSON object per run"
    )

    report = commands.add_parser("report", help="summarize metrics from all run folders")
    report.add_argument(
        "--json", action="store_true", help="print one machine-readable JSON object"
    )

    commands.add_parser("reindex", help="rebuild the index and verify finished run folders")

    approvals = commands.add_parser("approvals", help="list the approval queue")
    approvals.add_argument("--all", action="store_true", help="include decided entries")

    approve = commands.add_parser(
        "approve", help="approve an entry of the queue (extend, for a budget)"
    )
    approve.add_argument("id", help="the approval id, as `cplane approvals` lists it")
    approve.add_argument("--note", help="an optional note kept with the decision")

    deny = commands.add_parser("deny", help="deny an entry of the queue (end, for a budget)")
    deny.add_argument("id", help="the approval id, as `cplane approvals` lists it")
    deny.add_argument("--reason", required=True, help="why; the model sees this as its error")

    answer = commands.add_parser("answer", help="answer a question entry of the queue")
    answer.add_argument("id", help="the approval id, as `cplane approvals` lists it")
    answer.add_argument("text", help="the answer, given to the planner")

    inspect = commands.add_parser("inspect", help="show what happened in a run and why")
    inspect.add_argument("run_id", help="the run to inspect")
    inspect.add_argument("--step", help="one step folder (01-plan-1) or step name (plan)")
    inspect.add_argument(
        "--context",
        action="store_true",
        help="per step: files provided, files read, tokens by source",
    )
    inspect.add_argument(
        "--transcript", action="store_true", help="per agent step: the conversation"
    )

    commands.add_parser(
        "selfcheck", help="run a complete scripted flow on a fixture repository, offline"
    )

    self_update = commands.add_parser(
        "self-update", help="adopt a new version into the control checkout after a selfcheck"
    )
    self_update.add_argument(
        "--project",
        default=app_selfupdate.DEFAULT_PROJECT,
        help=f"the project whose checkouts to use (default: {app_selfupdate.DEFAULT_PROJECT})",
    )
    self_update.add_argument(
        "--to", help="the commit to adopt (default: main); a rollback is --to <old commit>"
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
    done = "Extended the budget for" if decided.answer == "extend" else "Approved"
    print(f"{done} {decided.id} (run {decided.run_id}).")
    return 0


def _deny(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    decided = asyncio.run(app_approvals.deny(home, args.id, reason=args.reason))
    done = "Ended" if decided.answer == "end" else "Denied"
    print(f"{done} {decided.id} (run {decided.run_id}): {decided.note}")
    return 0


def _answer(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    decided = asyncio.run(app_approvals.answer(home, args.id, args.text))
    print(f"Answered {decided.id} (run {decided.run_id}).")
    return 0


def _inspect(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    if args.transcript:
        for transcript in app_inspect.transcripts(home, args.run_id, args.step):
            print(f"== {transcript.step} ==")
            for message in transcript.messages:
                _print_message(message)
        return 0
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


def _print_message(message: Message) -> None:
    print(f"{message.role}:")
    for block in message.content:
        if block.type == "text":
            print(f"  [text] {block.text}")
        elif block.type == "tool_use":
            print(f"  [tool_use {block.name}] {json.dumps(block.input, sort_keys=True)}")
        else:
            mark = " error" if block.is_error else ""
            print(f"  [tool_result{mark}] {block.content}")


def _project(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    project = app_projects.add_project(home, args.name, Path(args.repo), protected=args.protected)
    print(f"Added project {args.name} at {project.repo}")
    print(f"Configure its gates in projects/{args.name}/project.yaml (docs/SPEC.md section 11).")
    return 0


def _task(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    if args.task_command == "list":
        return _task_list(home)
    if args.task_command == "show":
        print(app_tasks.find_task(home, args.id).path.read_text(encoding="utf-8"), end="")
        return 0
    fields = (args.title, args.project, args.goal, args.criterion)
    if args.file is not None:
        if any(fields):
            return _usage("task new takes --file or the fields of a task, not both")
        entry = asyncio.run(app_tasks.add_task_file(home, Path(args.file)))
    else:
        if not all(fields):
            return _usage("task new needs --title, --project, --goal and --criterion, or --file")
        entry = asyncio.run(
            app_tasks.new_task(
                home,
                title=args.title,
                project=args.project,
                goal=args.goal,
                criteria=args.criterion,
                priority=args.priority,
                plan_approval=args.plan_approval,
                lane=args.lane,
                max_iterations=args.max_iterations,
                max_cost_usd=args.max_cost,
            )
        )
    print(f"Created {entry.task.id}: {entry.relative(home)}")
    return 0


def _task_list(home: Home) -> int:
    entries = app_tasks.list_tasks(home)
    broken = app_tasks.broken_task_files(home)
    if not entries and not broken:
        print("No tasks.")
    for entry in entries:
        task = entry.task
        print(f"{task.id}  {task.status}  p{task.priority}  {task.project}  {task.title}")
    for problem in broken:
        print(f"cplane: not a task: {problem}", file=sys.stderr)
    return 1 if broken else 0


def _usage(message: str) -> int:
    print(f"cplane: {message}", file=sys.stderr)
    return USAGE_ERROR


_MODEL_PARKS = {
    REASON_CONNECTION: "the model endpoint could not be reached; check it and run {resume} "
    "after {when}.",
    REASON_SERVER_ERROR: "the model endpoint answered with server errors; run {resume} after "
    "{when}.",
    REASON_AUTH: f"the endpoint rejected the token; update {AUTH_TOKEN_VAR} and run {{resume}}.",
}
"""One line for a run parked on the model endpoint, by reason (docs/SPEC.md section 8.2)."""


def _report(outcome: RunOutcome) -> None:
    line = _MODEL_PARKS.get(outcome.reason or "") if outcome.status == "parked" else None
    if line is not None:
        when = outcome.resume_after.isoformat() if outcome.resume_after else "a moment"
        resume = f"`cplane resume {outcome.run_id}`"
        print(
            f"Run {outcome.run_id} of {outcome.task_id}: parked at {outcome.step}: "
            + line.format(resume=resume, when=when)
        )
        return
    print(
        f"Run {outcome.run_id} of {outcome.task_id}: {outcome.status} at {outcome.step}: "
        f"{outcome.message}"
    )
    if outcome.status == "parked" and outcome.approval_id:
        print(
            f"{_decide_hint(outcome.approval_id, outcome.approval_type)}, then "
            f"`cplane resume {outcome.run_id}`."
        )


def _decide_hint(entry: str, kind: ApprovalType | None) -> str:
    """How to decide a parked run's entry, with the command its type takes."""
    approve, deny = f"`cplane approve {entry}`", f"`cplane deny {entry} --reason ...`"
    if kind == "question":
        return f'Answer it with `cplane answer {entry} "..."`'
    if kind == "budget":
        return f"Extend the budget with {approve} or end the run with {deny}"
    return f"Decide it with {approve} or {deny}"


def _run(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    outcome = asyncio.run(app_runs.run_task(home, args.task_id, clients=make_clients(os.environ)))
    _report(outcome)
    return 1 if outcome.status == "failed" else 0


def _resume(args: argparse.Namespace) -> int:
    if (args.run_id is None) == (not args.all):
        return _usage("resume takes a run id or --all, not both and not neither")
    home = resolve_home(args)
    clients = make_clients(os.environ)
    if args.run_id is not None:
        outcome = asyncio.run(app_runs.resume_run(home, args.run_id, clients=clients))
        _report(outcome)
        return 1 if outcome.status == "failed" else 0
    results = asyncio.run(app_runs.resume_runs(home, clients=clients))
    if not results:
        print("No unfinished runs.")
    code = 0
    for result in results:
        if result.outcome is None:
            print(f"cplane: could not resume {result.run_id}: {result.error}", file=sys.stderr)
            code = 1
            continue
        _report(result.outcome)
        if result.outcome.status == "failed":
            code = 1
    return code


def _status(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    statuses = app_status.run_statuses(home, args.run_id)
    if args.json:
        for status in statuses:
            print(
                json.dumps(
                    {
                        "id": status.run_id,
                        "task": status.task_id,
                        "state": status.state,
                        "current_step": status.step,
                        "iteration": status.iteration,
                        "cost": status.cost_usd,
                    }
                )
            )
        return 0
    if not statuses:
        print("No runs.")
    for status in statuses:
        limit = f"/{status.max_iterations}" if status.max_iterations else ""
        line = (
            f"{status.run_id}  {status.task_id or '-'}  {status.state}  {status.step or '-'}  "
            f"iteration {status.iteration}{limit}  ${status.cost_usd:.4f}"
        )
        if status.approval_id:
            line += f"  waiting for {status.approval_id}"
        print(line)
    return 0


def _report_metrics(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    report = app_report.run_report(home)
    if args.json:
        print(json.dumps(report, separators=(",", ":")))
        return 0
    print(
        "Runs merged with no human input other than approvals: "
        f"{report['runs']['merged_without_human_input']}"
    )
    print(f"Median iterations: {report['iterations']['median']}")
    print(f"Median cost: ${report['cost_usd']['median']}")
    print(f"Total cost: ${report['cost_usd']['total']}")
    wall = report["wall_minutes"]["median"]
    print(f"Median wall time: {wall:.1f} minutes" if wall is not None else "Median wall time: None")
    print("Gate failures:")
    for name, count in report["gate_failures"].items():
        print(f"  {name}: {count}")
    print("Tool calls:")
    for class_name, decisions in report["tool_calls"].items():
        for decision, count in decisions.items():
            print(f"  {class_name} / {decision}: {count}")
    return 0


def _selfcheck(args: argparse.Namespace) -> int:
    report = asyncio.run(app_selfcheck.run_selfcheck())
    for check in report.checks:
        print(f"{'ok' if check.ok else 'FAIL':<5} {check.name}: {check.detail}")
    print("selfcheck passed" if report.ok else "selfcheck FAILED")
    return 0 if report.ok else 1


def _self_update(args: argparse.Namespace) -> int:
    home = resolve_home(args)
    result = asyncio.run(app_selfupdate.self_update(home, args.project, to=args.to))
    if result.failed == "selfcheck":
        print(result.output, end="")
        print(f"cplane: selfcheck failed in {result.primary}; nothing changed", file=sys.stderr)
        return 1
    if result.failed == "sync":
        print(result.output, end="")
        print(
            f"cplane: uv sync failed in {result.control}; {app_selfupdate.TAG} and the control "
            f"checkout are back at {result.previous or 'their previous commit'}",
            file=sys.stderr,
        )
        return 1
    print(f"selfcheck passed in {result.primary}")
    print(f"{app_selfupdate.TAG}: {result.previous or '(none)'} -> {result.current}")
    print(f"control checkout {result.control} is at {result.current}")
    return 0


COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "init": _init,
    "project": _project,
    "task": _task,
    "run": _run,
    "resume": _resume,
    "status": _status,
    "report": _report_metrics,
    "selfcheck": _selfcheck,
    "self-update": _self_update,
    "reindex": _reindex,
    "approvals": _approvals,
    "approve": _approve,
    "deny": _deny,
    "answer": _answer,
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
