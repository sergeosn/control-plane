# Control Plane

Control Plane is a local, single-user system that takes a task from a backlog to a merged
branch. For each task it assembles context, plans, carries out the plan in an isolated copy of
the repository, checks the result with deterministic gates, has a second model review it,
iterates on the feedback, and stops only where a human decision is required. The first project
it works on is its own source code.

The specification is [docs/SPEC.md](docs/SPEC.md) and the task plan is
[docs/TASKS.md](docs/TASKS.md).

## Install

You need [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
uv sync
```

This creates the environment and installs the package in editable mode, with the `cplane`
command. Run the project's checks:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov
```

`uv run cplane selfcheck` runs a complete scripted flow against a fixture repository, with no
network, and prints one line per check.

## Run

Every command finds the home repository through `--home` or `CPLANE_HOME`. Model credentials
come from the environment. For an OpenAI-compatible gateway such as a LiteLLM proxy:

```bash
export CPLANE_HOME=~/plane-home
export CPLANE_LLM_BASE_URL=https://your-gateway.example.com
export CPLANE_LLM_AUTH_TOKEN=...
```

The engine never reads `ANTHROPIC_*` or `OPENAI_*` variables. Model profiles, the price table
and default budgets live in `plane.yaml` in the home repository.

A first task, from an empty home:

```bash
uv run cplane init ~/plane-home
uv run cplane project add control-plane --repo /path/to/control-plane
uv run cplane task new --project control-plane --title "..." --goal "..." --criterion "..."
uv run cplane run T-0001
```

A run goes until it finishes or parks on an approval. Then:

```bash
uv run cplane approvals
uv run cplane approve <approval-id>
uv run cplane resume <run-id>
```

`approve` and `deny` decide `plan`, `tool_call`, `merge` and `budget` entries (for `budget`,
`approve` extends the budget and `deny` ends the run). `answer` replies to a `question`.
`cplane status [--json]` shows each run's state, step, iteration and cost, and
`cplane inspect <run-id>` shows what the models were given and what they did. The full command
list is in [docs/SPEC.md](docs/SPEC.md) section 14.

## First live run

Run `R-20261118-180921-03c1` was the first task delivered through `cplane run` against a real
model.

| | |
|---|---|
| Task | T-0001: add `--json` output to `cplane status` |
| Model | `bedrock_mantle/openai/gpt-5.6-luna` for planner, worker and reviewer, through a LiteLLM proxy |
| Iterations | 1 (gates and review passed on the first attempt) |
| Cost | $1.48, by the engine's own estimate from the price table in `plane.yaml` |
| Human input | One budget extension and one merge approval |
| Result | `done`; merged into `main` as commit `06d334f` |

Cost per step: plan $0.30, implement (first attempt) $0.51, implement (second attempt) $0.45,
review $0.22.

### What went wrong

- **The worker hit its token budget.** The first implement attempt used 407,642 tokens against
  the 400,000 step budget (`max_total_tokens`) in 17 model calls, failed, and parked the run on
  a `budget` approval. Every call resends the whole conversation so far, so the total grows
  with the square of the number of turns, and a modest edit can exhaust a budget sized for
  fresh tokens. After the extension, the second attempt finished the change in 10 calls.
- **The CLI could not answer a budget approval.** A `budget` entry takes `extend` or `end`, but
  `cplane approve` sent `approve`, which that entry does not accept. Fixed in `0de41c4`:
  `approve` and `deny` now send the positive and negative answer of each approval type.
