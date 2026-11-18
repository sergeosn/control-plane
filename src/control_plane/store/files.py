"""File primitives of the store (docs/SPEC.md section 5).

Whole-file writes, JSON among them, go to a temporary name in the same directory and are
renamed into place, so a reader never sees a half-written file and a crash leaves no partial
file behind.
JSONL files are append-only: one record per line, never rewritten.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from control_plane.domain.base import DomainModel


def write_json_atomic(path: Path, data: DomainModel | Mapping[str, Any]) -> None:
    """Write `data` as the content of a `.json` file, atomically (see `write_text_atomic`)."""
    text = data.to_json() if isinstance(data, DomainModel) else json.dumps(data, indent=2) + "\n"
    write_text_atomic(path, text)


def write_text_atomic(path: Path, text: str) -> None:
    """Write `text`, encoded as UTF-8, as the whole content of `path` (see `write_bytes_atomic`)."""
    write_bytes_atomic(path, text.encode("utf-8"))


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write `data` as the whole content of `path`, atomically.

    On failure the target is untouched (it keeps its previous content if it existed) and the
    temporary file is removed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, record: DomainModel | str) -> None:
    """Append one record to a `.jsonl` file. A record is one line; nothing is ever rewritten."""
    text = record.to_jsonl_line() if isinstance(record, DomainModel) else record
    if not text.endswith("\n"):
        text += "\n"
    if "\n" in text[:-1]:
        raise ValueError("a JSONL record must be one line")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl[M: DomainModel](path: Path, model: type[M]) -> list[M]:
    """Every record of a `.jsonl` file as `model`; a missing file holds no records."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [model.from_jsonl_line(line) for line in handle if line.strip()]
