# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    SourceRef,
    TextEvidence,
    manifest_for_definition,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PiEventInput,
    PiEventSource,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

HEADER = {"type": "session", "version": 3, "id": "session-1"}


def _content(records: list[dict[str, object]], header: dict[str, object] = HEADER) -> bytes:
    return "\n".join(json.dumps(record) for record in [header, *records]).encode()


def _parse(records: list[dict[str, object]], leaf: str | None = None) -> tuple[PiEventInput, ...]:
    return parse_pi_session(PiSessionInput(content=_content(records), leaf_id=leaf))


def test_selected_branch_renders_every_visible_body_shape() -> None:
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "request"}},
        {"type": "thinking_level_change", "id": "control", "parentId": "root"},
        {"type": "compaction", "id": "summary", "parentId": "control", "summary": "compacted"},
        {
            "type": "message",
            "id": "tools",
            "parentId": "summary",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "first"},
                    {
                        "type": "toolCall",
                        "name": "read",
                        "id": "call-1",
                        "arguments": {"z": 1, "a": ["é", 2]},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "tools",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "contents"}],
            },
        },
        {
            "type": "message",
            "id": "bash",
            "parentId": "result",
            "message": {"role": "bashExecution", "command": "echo hi", "output": "hi", "content": []},
        },
        {"type": "custom_message", "id": "extra", "parentId": "bash", "content": "note"},
    ]
    events = _parse(records, "extra")

    assert [event.event_id for event in events] == ["root", "summary", "tools", "result", "bash", "extra"]
    assert [event.content for event in events] == [
        "user: request",
        "compaction: compacted",
        'assistant: first\n[tool_call read call-1] {"a":["é",2],"z":1}',
        "toolResult: [tool_result read call-1] contents",
        "bashExecution: $ echo hi\nhi",
        "custom: note",
    ]
    assert events[0].parent_id is None
    assert events[1].parent_id == "control"
    assert events[0].event_type == "message" and events[1].event_type == "compaction"


def test_empty_or_hidden_bodies_are_ignored_and_default_leaf_is_last_entry() -> None:
    records = [
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "assistant", "content": []}},
        {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "content": "visible"}},
        {"type": "model_change", "id": "c", "parentId": "b", "provider": "x", "modelId": "y"},
    ]
    assert [event.event_id for event in _parse(records)] == ["b"]
    assert _parse(records, "a") == ()


def test_identity_projection_and_read_round_trip() -> None:
    events = _parse([
        {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "hello"}},
    ])
    registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(events[0]))

    assert isinstance(source, PiEventSource)
    assert source.source_id == source.name
    assert source.source_id.startswith("pi:")
    assert asyncio.run(registry.read(source)) == events[0]

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "pi-event"
    assert evidence.source_id == source.source_id
    assert evidence.content == "user: hello"
    assert evidence.metadata == {
        "session_id": "session-1",
        "event_id": "a",
        "parent_id": None,
        "event_type": "message",
        "role": "user",
    }
    assert manifest_for_definition(PI_EVENT_SOURCE_DEFINITION).name == "pi-event"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"\xff",
        b'{"type":"session","version":2,"id":"x"}\n',
        b'{"type":"session","version":3,"id":""}\n',
        b'{"type":"session","version":3,"id":"x"}\nnot json\n',
        b'{"type":"session","version":3,"id":"x"}\n[]\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":1}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":"later"}\n'
        b'{"type":"message","id":"later","parentId":null}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":null}\n'
        b'{"type":"message","id":"a","parentId":null}\n',
    ],
)
def test_invalid_archives_raise_value_error(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_missing_leaf_and_header_only_sessions_raise_value_error() -> None:
    with pytest.raises(ValueError):
        _parse([{"type": "message", "id": "a", "parentId": None}], "absent")
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=_content([])))


class _Sink:
    def __init__(self) -> None:
        self.item_ids: list[str] = []

    async def submit(self, binding, item_id, definition_name, value, /):
        assert definition_name == "pi-event"
        assert isinstance(value, PiEventInput)
        self.item_ids.append(item_id)
        return SourceRef(source_type=definition_name, source_id=f"pi:{item_id}")


class _Checkpoints:
    def __init__(self) -> None:
        self.value = None
        self.saves = 0

    async def load(self, binding, /):
        return self.value

    async def save(self, binding, checkpoint, /, *, expected):
        assert expected == self.value
        self.value = checkpoint
        self.saves += 1


def test_connector_switches_branches_and_rejects_rewritten_history(tmp_path: Path) -> None:
    path = tmp_path / "tree.jsonl"
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
        {"type": "message", "id": "left", "parentId": "root", "message": {"role": "assistant", "content": "left"}},
        {"type": "model_change", "id": "control", "parentId": "root"},
        {"type": "message", "id": "right", "parentId": "control", "message": {"role": "assistant", "content": "right"}},
    ]
    path.write_bytes(_content(records))
    binding = ConnectorBinding(
        scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )
    sink, store = _Sink(), _Checkpoints()
    lifecycle = ConnectorLifecycle(sink=sink, checkpoints=store)

    async def run(leaf: str | None):
        sink.item_ids.clear()
        return await lifecycle.run(PiSessionConnector(path, leaf), binding)

    first = asyncio.run(run("left"))
    assert [outcome.item_id for outcome in first.items] == ["root", "left"]
    assert first.committed_checkpoint == store.value

    second = asyncio.run(run("right"))
    assert [outcome.item_id for outcome in second.items] == ["right"]
    assert second.previous_checkpoint == first.committed_checkpoint

    replay = asyncio.run(run("right"))
    assert replay.items == ()
    assert store.saves == 2

    back = asyncio.run(run("left"))
    assert back.items == ()

    records[1]["message"]["content"] = "left rewritten"
    path.write_bytes(_content(records))
    with pytest.raises(ValueError):
        asyncio.run(run("right"))
    assert store.saves == 2
    records[1]["message"]["content"] = "left"

    records[0]["message"]["content"] = "rewritten"
    path.write_bytes(_content(records))
    with pytest.raises(ValueError):
        asyncio.run(run("right"))
    assert store.saves == 2

    records[0]["message"]["content"] = "shared"
    path.write_bytes(_content(records))
    repaired = asyncio.run(run("right"))
    assert repaired.items == () and repaired.previous_checkpoint == second.committed_checkpoint
