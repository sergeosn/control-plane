"""`transcript.jsonl` (docs/SPEC.md section 8.6).

One record per line, each a `Message`. The loop appends a record after every model response
and after every tool result, so the file is current at every moment: a user turn that
answers several `tool_use` blocks is several records. On reload, consecutive records of the
same role merge into one message, which is the conversation as the model saw it.
"""

from pathlib import Path

from control_plane.llm.types import Message
from control_plane.store.files import append_jsonl


def transcript_line(message: Message) -> str:
    return message.model_dump_json() + "\n"


def append_transcript(path: Path, message: Message) -> None:
    append_jsonl(path, transcript_line(message))


def load_transcript(path: Path) -> list[Message]:
    """The messages of a transcript; a missing file holds none."""
    if not path.exists():
        return []
    messages: list[Message] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = Message.model_validate_json(line)
            if messages and messages[-1].role == record.role:
                messages[-1] = Message(
                    role=record.role, content=[*messages[-1].content, *record.content]
                )
            else:
                messages.append(record)
    return messages
