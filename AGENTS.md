# AGENTS.md

Control Plane (package `control_plane`, CLI `cplane`) takes a task from a backlog to a merged
branch. The specification is `SPEC.md`; the task plan is `TASKS.md`. Section numbers in tasks
refer to `SPEC.md`.

## Package layout

`src/control_plane/` (SPEC.md section 17):

| Package      | Holds                                                                     |
|--------------|---------------------------------------------------------------------------|
| `cli.py`     | The `cplane` command line. Calls `app`; never writes files or the index.  |
| `app/`       | Application services, the only code that writes                           |
| `domain/`    | Pydantic models: task, plan, work report, review, manifest, events, approvals |
| `store/`     | Run folders, home repository, index                                       |
| `llm/`       | `LLMClient` protocol, OpenAI-compatible client, scripted client, pricing  |
| `agent/`     | The tool loop, budgets, loop detection                                    |
| `tools/`     | Tool definitions and the executor                                         |
| `policy/`    | Classification, path jail, protected paths, parking                       |
| `context/`   | Brief, index, repo map, manifest                                          |
| `workspace/` | Worktrees, diff, merge                                                    |
| `gates/`     | Deterministic checks                                                      |
| `flow/`      | Schema, validation, state machine                                         |

`tests/`: `unit/`, `integration/`, `guardrails/` (the guardrail regression suite, section 16),
`fixtures/`. `tests/conftest.py` blocks network sockets for every test.

## Running the checks

The project uses `uv` on Python 3.12 or newer. `uv sync` creates the environment and installs
the package in editable mode. CI (`.github/workflows/ci.yml`) runs exactly these three commands:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest --cov
```

`pytest --cov` fails below 85% coverage (`fail_under` in `pyproject.toml`). Formatting fixes:
`uv run ruff format .`.

## Standing rules

1. **No network in tests.** Tests never call a model or the network. `ScriptedClient` replays
   prepared responses. `tests/conftest.py` makes any socket connection raise; do not disable it.
2. **No new dependency without a stated reason.** Add a dependency only when the task needs it,
   and say in the commit or report why the standard library or an existing dependency does not
   do the job.
3. **Files first and index second.** Run folders and the home repository are the source of
   truth. The SQLite index is derived from them and can be deleted and rebuilt. A file write
   never fails because an index write failed.

Also: every task is done when its acceptance checks pass as automated tests, `ruff check` and
`ruff format --check` are clean, coverage stays at or above 85%, and `SPEC.md` is updated if the
implementation had to differ from it. Write the tests for each "Done when" item before the code.
