---
id: T-0042
title: Resume every parked run whose approvals are decided with `cplane resume --all`
project: control-plane
lane: development
priority: 1
plan_approval: true
roles:
  planner: default
  worker: default
  reviewer: strict
budget:
  max_iterations: 4
  max_cost_usd: 8.00
status: backlog
---

## Goal

A human who has worked through the approval queue should not have to resume each parked run
by hand. `cplane resume --all` finds every run that is parked, checks whether every approval
entry it waits on now carries a decision, and resumes those runs one after another in the
foreground, oldest first. A run that still waits on an undecided entry is skipped and named
in the output with the ids it waits on, so the human knows what is left to decide.

Rate-limited runs are parked without an approval entry and carry `resume_after` in the
state of the parked step. They are resumed only when that time has passed; before that they
are skipped and the output says when they can be tried again. A run that fails while being
resumed does not stop the others: its failure is reported, the remaining runs are resumed,
and the command exits non-zero at the end so that a script calling it notices.

The command must be safe to run twice. A run that another process is already resuming is
skipped with a message rather than resumed a second time, which means the resume takes a
lock in the run folder and releases it when the run parks again, finishes or fails. A stale
lock left by a crashed process is detected by its process id and removed with a warning.

Nothing about single-run resume changes: `cplane resume <run-id>` keeps its current output
and exit codes, and the new code path reuses it rather than duplicating it.

## Acceptance criteria

- [AC1] `cplane resume --all` resumes every parked run whose approval entries are all decided, oldest first.
- [AC2] A run with an undecided entry is skipped and named with the ids it waits on.
- [AC3] A rate-limited run is resumed only after its `resume_after` time, and skipped with that time before it.
- [AC4] A failure while resuming one run is reported, the other runs are still resumed, and the exit code is non-zero.
- [AC5] A run that is already being resumed by another process is skipped with a message.
- [AC6] A stale lock from a crashed process is removed with a warning and the run is resumed.
- [AC7] `cplane resume <run-id>` keeps its output and exit codes, covered by its existing tests.
- [AC8] `--all` together with a run id is rejected with a usage error.
- [AC9] Every path above is covered by a test that uses the scripted client.
