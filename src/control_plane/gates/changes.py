"""The gates that read the diff: `diff_size` and `secrets` (docs/SPEC.md section 11).

Both see what `diff.patch` holds: every change in the worktree against the base commit.
The secrets gate reports where a match is and which pattern matched, never the matched text,
because its output is given to the next worker and to the reviewer.
"""

import re
from collections.abc import Mapping

from control_plane.domain.project import DiffSizeGate, SecretsGate
from control_plane.gates.base import GateResult, gate_result
from control_plane.policy.protected import PathPatterns
from control_plane.workspace.diff import added_lines, changed_files
from control_plane.workspace.worktree import Worktree

DEFAULT_SECRET_PATTERNS: dict[str, str] = {
    "aws_access_key_id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "private_key": r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})",
    "anthropic_api_key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "openai_api_key": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9]{20,}",
    "slack_token": r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    "google_api_key": r"\bAIza[0-9A-Za-z_-]{35}",
    "credential_assignment": (
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|auth[_-]?token|token)\b"
        r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
    ),
}


def secret_matches(line: str, patterns: Mapping[str, str]) -> list[str]:
    """The names of the patterns that match `line`."""
    return [name for name, pattern in patterns.items() if re.search(pattern, line)]


def diff_size_gate(gate: DiffSizeGate, tree: Worktree) -> GateResult:
    changes = sorted(changed_files(tree), key=lambda c: (-c.changed_lines, c.path))
    total = sum(change.changed_lines for change in changes)
    summary = f"changed lines: {total} (limit {gate.max_changed_lines})"
    listing = "\n".join(
        f"{change.path}: binary"
        if change.binary
        else f"{change.path}: +{change.added} -{change.deleted}"
        for change in changes
    )
    status = "fail" if total > gate.max_changed_lines else "pass"
    return gate_result("diff_size", status, summary, listing)


def secrets_gate(gate: SecretsGate, tree: Worktree) -> GateResult:
    patterns = {**DEFAULT_SECRET_PATTERNS, **gate.patterns}
    ignored = PathPatterns(gate.ignore_paths)
    findings = []
    for added in added_lines(tree):
        if ignored.matches(added.path):
            continue
        names = secret_matches(added.text, patterns)
        if names:
            findings.append(f"{added.path}:{added.line}: {', '.join(names)}")
    if findings:
        summary = f"{len(findings)} added lines match a secret pattern"
        detail = "The matched text is not repeated here.\n\n" + "\n".join(findings)
        return gate_result("secrets", "fail", summary, detail)
    return gate_result("secrets", "pass", f"no added line matches {len(patterns)} patterns")
