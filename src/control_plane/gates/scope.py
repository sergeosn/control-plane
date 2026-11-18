"""The `scope` check: the diff against the plan's `files_to_touch` (docs/SPEC.md section 11).

It is not a gate and never fails the run. The files it finds go into the reviewer's context,
which judges whether each change outside the plan is justified. A plan entry is a file, a
directory or a glob, matched as protected paths are. A rename touches both of its paths.
"""

from collections.abc import Iterable

from control_plane.policy.protected import PathPatterns
from control_plane.workspace.diff import FileChange


def out_of_plan(files_to_touch: Iterable[str], changes: Iterable[FileChange]) -> list[str]:
    planned = PathPatterns(files_to_touch)
    touched = set()
    for change in changes:
        touched.add(change.path)
        if change.old_path is not None:
            touched.add(change.old_path)
    return sorted(path for path in touched if not planned.matches(path))
