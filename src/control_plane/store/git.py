"""A thin wrapper around the `git` command for the home repository.

Commits made here are the engine's own bookkeeping, so they carry a fixed engine identity
and are never signed. Nothing in this module talks to a remote.
"""

import os
import subprocess
from pathlib import Path

ENGINE_NAME = "Control Plane"
ENGINE_EMAIL = "cplane@localhost"

_STATUS_KINDS = {"?": "added", "A": "added", "D": "deleted"}


class GitError(RuntimeError):
    """A git command failed; the message carries git's own output."""


class Git:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _process(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": ENGINE_NAME,
            "GIT_AUTHOR_EMAIL": ENGINE_EMAIL,
            "GIT_COMMITTER_NAME": ENGINE_NAME,
            "GIT_COMMITTER_EMAIL": ENGINE_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
        }
        return subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _run(self, *args: str) -> str:
        result = self._process(*args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed in {self.root}: {detail}")
        return result.stdout

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._run("init", "--quiet", "--initial-branch=main")

    def add(self, *paths: str) -> None:
        self._run("add", "--all", "--", *paths)

    def commit(self, message: str) -> str:
        """Commit what is staged and return the new head's sha."""
        self._run("commit", "--quiet", "--message", message)
        return self.head()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD").strip()

    def head_author(self) -> str:
        return self._run("log", "-1", "--format=%an <%ae>").strip()

    def current_branch(self) -> str:
        return self._run("symbolic-ref", "--short", "HEAD").strip()

    def log_subjects(self) -> list[str]:
        """Commit subjects, newest first."""
        return self._run("log", "--format=%s").splitlines()

    def status(self, *paths: str) -> list[tuple[str, str]]:
        """Working-tree changes under `paths` as `(kind, path)`, kind in added/modified/deleted."""
        out = self._run("status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
        changes = []
        for line in out.splitlines():
            code, _, path = line[:2], line[2], line[3:]
            kind = _STATUS_KINDS.get(code[0]) or _STATUS_KINDS.get(code[1]) or "modified"
            changes.append((kind, path.split(" -> ")[-1]))
        return changes

    def tracked(self, path: str) -> list[str]:
        """Paths committed or staged under `path`."""
        return self._run("ls-files", "--", path).splitlines()

    def is_ignored(self, path: str) -> bool:
        result = self._process("check-ignore", "--quiet", "--", path)
        if result.returncode not in (0, 1):
            raise GitError(f"git check-ignore failed in {self.root}: {result.stderr.strip()}")
        return result.returncode == 0
