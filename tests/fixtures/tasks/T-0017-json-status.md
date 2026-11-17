---
id: T-0017
title: Add --json output to `cplane status`
project: control-plane
lane: development
priority: 2
plan_approval: false
roles:
  planner: default
  worker: default
  reviewer: strict
budget:
  max_iterations: 3
  max_cost_usd: 4.00
status: backlog
---

## Goal

`cplane status` can print machine-readable output.

## Acceptance criteria

- [AC1] `cplane status --json` prints one JSON object per run with id, task, state and cost.
- [AC2] Without the flag the output is unchanged.
- [AC3] Both paths are covered by tests.
