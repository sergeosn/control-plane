"""Task 8: `transcript.jsonl` holds one record per message part; consecutive records of the
same role form one message when the transcript is reloaded (SPEC.md section 8.6)."""

from pathlib import Path

from control_plane.agent import append_transcript, load_transcript
from control_plane.llm import Message, TextBlock, ToolResultBlock, ToolUseBlock


def result(tool_id: str) -> Message:
    return Message(
        role="user", content=[ToolResultBlock(tool_use_id=tool_id, content=f"result {tool_id}")]
    )


def test_a_missing_transcript_holds_no_messages(tmp_path: Path) -> None:
    assert load_transcript(tmp_path / "transcript.jsonl") == []


def test_messages_round_trip_one_record_per_line(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    assistant = Message(
        role="assistant", content=[ToolUseBlock(id="t1", name="read_file", input={"path": "a"})]
    )

    append_transcript(path, Message.user("brief"))
    append_transcript(path, assistant)

    assert len(path.read_text().splitlines()) == 2
    assert load_transcript(path) == [Message.user("brief"), assistant]


def test_consecutive_records_of_the_same_role_merge_into_one_message(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    append_transcript(path, Message.user("brief"))
    append_transcript(path, Message(role="assistant", content=[TextBlock(text="calling")]))
    append_transcript(path, result("t1"))
    append_transcript(path, result("t2"))

    messages = load_transcript(path)

    assert [m.role for m in messages] == ["user", "assistant", "user"]
    assert [b.tool_use_id for b in messages[-1].content] == ["t1", "t2"]
