"""A thin wrapper around the `git` command, for the home repository and project checkouts.

Commits made here are the engine's own bookkeeping, so they carry a fixed engine identity
and are never signed. Nothing in this module talks to a remote.
"""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

ENGINE_NAME = "Control Plane"
ENGINE_EMAIL = "cplane@localhost"

_STATUS_KINDS = {"?": "added", "A": "added", "D": "deleted"}


class GitError(RuntimeError):
    """A git command failed; the message carries git's own output."""


class Git:
    def __init__(self, root: Path, *, config: Mapping[str, str] | None = None) -> None:
        """`config` is passed as `-c name=value` to every command run through this instance."""
        self.root = root
        self._config = {"commit.gpgsign": "false", **(config or {})}

    def process(
        self, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run `git args` and return the finished process, whatever its exit code."""
        return subprocess.run(
            self._argv(args),
            cwd=self.root,
            env=self._env(env),
            capture_output=True,
            text=True,
            check=False,
        )

    def run(self, *args: str, env: Mapping[str, str] | None = None) -> str:
        """Run `git args` and return its stdout; a non-zero exit raises `GitError`."""
        result = self.process(*args, env=env)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed in {self.root}: {detail}")
        return result.stdout

    def run_bytes(self, *args: str, env: Mapping[str, str] | None = None) -> bytes:
        """Like `run`, for output that must be kept byte for byte, such as a patch."""
        result = subprocess.run(
            self._argv(args),
            cwd=self.root,
            env=self._env(env),
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise GitError(f"git {' '.join(args)} failed in {self.root}: {detail}")
        return result.stdout

    def _argv(self, args: tuple[str, ...]) -> list[str]:
        options = [
            part for name, value in self._config.items() for part in ("-c", f"{name}={value}")
        ]
        return ["git", *options, *args]

    def _env(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": ENGINE_NAME,
            "GIT_AUTHOR_EMAIL": ENGINE_EMAIL,
            "GIT_COMMITTER_NAME": ENGINE_NAME,
            "GIT_COMMITTER_EMAIL": ENGINE_EMAIL,
            "GIT_TERMINAL_PROMPT": "0",
            **(extra or {}),
        }

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.run("init", "--quiet", "--initial-branch=main")

    def add(self, *paths: str) -> None:
        self.run("add", "--all", "--", *paths)

    def commit(self, message: str) -> str:
        """Commit what is staged and return the new head's sha."""
        self.run("commit", "--quiet", "--message", message)
        return self.head()

    def commit_paths(self, message: str, *paths: str) -> str:
        """Stage and commit `paths` only, leaving anything else staged as it is."""
        self.add(*paths)
        self.run("commit", "--quiet", "--message", message, "--", *paths)
        return self.head()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD").strip()

    def head_author(self) -> str:
        return self.run("log", "-1", "--format=%an <%ae>").strip()

    def current_branch(self) -> str:
        return self.run("symbolic-ref", "--short", "HEAD").strip()

    def log_subjects(self) -> list[str]:
        """Commit subjects, newest first."""
        return self.run("log", "--format=%s").splitlines()

    def status(self, *paths: str) -> list[tuple[str, str]]:
        """Working-tree changes under `paths` as `(kind, path)`, kind in added/modified/deleted."""
        out = self.run("status", "--porcelain=v1", "--untracked-files=all", "--", *paths)
        changes = []
        for line in out.splitlines():
            code, _, path = line[:2], line[2], line[3:]
            kind = _STATUS_KINDS.get(code[0]) or _STATUS_KINDS.get(code[1]) or "modified"
            changes.append((kind, path.split(" -> ")[-1]))
        return changes

    def tracked(self, path: str) -> list[str]:
        """Paths committed or staged under `path`."""
        return self.run("ls-files", "--", path).splitlines()

    def is_ignored(self, path: str) -> bool:
        result = self.process("check-ignore", "--quiet", "--", path)
        if result.returncode not in (0, 1):
            raise GitError(f"git check-ignore failed in {self.root}: {result.stderr.strip()}")
        return result.returncode == 0
