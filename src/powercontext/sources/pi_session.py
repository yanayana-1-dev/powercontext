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

"""Raw Pi session history captured from one selected branch.

A Pi session file is a JSONL tree of entries linked by ``id``/``parentId``.
The selected leaf determines one history path; every visible entry on that
path becomes an immutable ``CAPTURED`` Source. Control entries stay in the
tree only to preserve parent links and never produce text evidence.

``PiSessionConnector`` re-parses the whole file on each run and submits only
events that are not yet recorded in its durable checkpoint. The checkpoint
stores a digest per imported event so a rewritten event is rejected instead
of silently replacing the original evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, JsonValue, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_SOURCE_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"
PI_SESSION_FORMAT_VERSION = 3
_CHECKPOINT_VERSION = 1


class PiSessionInput(BaseModel):
    """One complete Pi session archive and the branch end to select."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected Pi session branch."""

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi session event addressed by its session and event ids."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="before")
    @classmethod
    def derive_identity(cls, data: Any) -> Any:
        """Fill the event identity from its components when callers omit it."""

        if not isinstance(data, dict):
            return data
        fields = dict(data)
        source_id = fields.get("source_id")
        if source_id is None and isinstance(fields.get("session_id"), str) and isinstance(fields.get("event_id"), str):
            source_id = _pi_event_source_id(fields["session_id"], fields["event_id"])
            fields["source_id"] = source_id
        if fields.get("name") is None and isinstance(source_id, str):
            fields["name"] = source_id
        return fields

    @model_validator(mode="after")
    def validate_identity(self) -> PiEventSource:
        if not self.source_id or not self.source_id.strip():
            raise ValueError("source_id must be a non-empty string")  # noqa: TRY003
        if self.source_id != self.source_id.strip():
            raise ValueError("source_id must not contain leading or trailing whitespace")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")  # noqa: TRY003
        if len(self.source_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
        return self


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Return the visible events on ``value.leaf_id`` from root to leaf."""

    session_id, nodes = _read_session(value.content)
    branch = _select_branch(nodes, value.leaf_id)
    return tuple(event for node in branch if (event := _visible_event(session_id, node)) is not None)


class PiEventTextEvidenceProjection:
    """Expose one captured Pi event as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiEventSourceAdapter:
    """Resolve and read Pi session event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            source_id=_pi_event_source_id(value.session_id, value.event_id),
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_SOURCE_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Incrementally capture the visible events of one selected Pi branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        """Submit events that are not yet checkpointed and reject rewrites."""

        session_id, nodes = _read_session(self.path.read_bytes())
        branch = _select_branch(nodes, self.leaf_id)
        imported = _checkpoint_events(session.checkpoint, session_id)
        digests = {node.event_id: _record_digest(node.record) for node in nodes}
        for event_id, digest in imported.items():
            current = digests.get(event_id)
            if current is not None and current != digest:
                raise ValueError(f"Pi event {event_id!r} changed after it was imported")  # noqa: TRY003

        events = tuple(event for node in branch if (event := _visible_event(session_id, node)) is not None)
        pending = [event for event in events if event.event_id not in imported]
        for event in pending:
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)

        merged = dict(imported)
        merged.update({event.event_id: digests[event.event_id] for event in events})
        checkpoint = {
            "version": _CHECKPOINT_VERSION,
            "session_id": session_id,
            "events": {event_id: merged[event_id] for event_id in sorted(merged)},
        }
        return ConnectorRunCompletion(status=ConnectorRunStatus.COMPLETE, checkpoint=checkpoint)


@dataclass(frozen=True, slots=True)
class _PiNode:
    event_id: str
    parent_id: str | None
    record: dict[str, JsonValue]


def _pi_event_source_id(session_id: str, event_id: str) -> str:
    source_id = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"Pi event identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def _decode_lines(content: bytes) -> list[str]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("Pi session must contain a session header")  # noqa: TRY003
    return lines


def _decode_json(line: str) -> JsonValue:
    try:
        return json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError("Pi session line is not valid JSON") from error  # noqa: TRY003


def _parse_header(line: str) -> str:
    header = _decode_json(line)
    if not isinstance(header, dict) or header.get("type") != "session":
        raise ValueError("Pi session must start with a session header")  # noqa: TRY003
    if header.get("version") != PI_SESSION_FORMAT_VERSION:
        raise ValueError(f"Pi session version must be {PI_SESSION_FORMAT_VERSION}")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session header must declare a non-empty id")  # noqa: TRY003
    return session_id


def _parse_nodes(lines: list[str]) -> tuple[_PiNode, ...]:
    nodes: list[_PiNode] = []
    seen: set[str] = set()
    for line in lines:
        record = _decode_json(line)
        if not isinstance(record, dict):
            raise ValueError("Pi session entries must be JSON objects")  # noqa: TRY003, TRY004
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi session entries must declare a non-empty id")  # noqa: TRY003
        if event_id in seen:
            raise ValueError(f"duplicate Pi session entry id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id is not None and (not isinstance(parent_id, str) or parent_id not in seen):
            raise ValueError(f"Pi session entry {event_id!r} references an unknown parent")  # noqa: TRY003
        seen.add(event_id)
        nodes.append(_PiNode(event_id=event_id, parent_id=parent_id, record=record))
    return tuple(nodes)


def _read_session(content: bytes) -> tuple[str, tuple[_PiNode, ...]]:
    lines = _decode_lines(content)
    return _parse_header(lines[0]), _parse_nodes(lines[1:])


def _select_branch(nodes: tuple[_PiNode, ...], leaf_id: str | None) -> tuple[_PiNode, ...]:
    if not nodes:
        raise ValueError("Pi session has no entries to select a leaf from")  # noqa: TRY003
    by_id = {node.event_id: node for node in nodes}
    leaf = nodes[-1].event_id if leaf_id is None else leaf_id
    if leaf not in by_id:
        raise ValueError(f"Pi session leaf {leaf!r} does not exist")  # noqa: TRY003
    branch: list[_PiNode] = []
    current = by_id[leaf]
    while True:
        branch.append(current)
        if current.parent_id is None:
            break
        current = by_id[current.parent_id]
    branch.reverse()
    return tuple(branch)


def _visible_event(session_id: str, node: _PiNode) -> PiEventInput | None:
    record = node.record
    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(role, message)
    elif event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            return None
        role = event_type
        body = summary
    elif event_type == "custom_message":
        role = "custom"
        body = _content_body(record.get("content"))
    else:
        return None
    if not body:
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=node.event_id,
        parent_id=node.parent_id,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(role: str, message: dict[str, JsonValue]) -> str:
    if role == "bashExecution":
        command = message.get("command")
        output = message.get("output")
        command = command if isinstance(command, str) else ""
        output = output if isinstance(output, str) else ""
        return f"$ {command}\n{output}" if output else f"$ {command}"
    if role == "toolResult":
        text = _content_body(message.get("content"))
        tool_name = message.get("toolName")
        tool_call_id = message.get("toolCallId")
        name = tool_name if isinstance(tool_name, str) and tool_name else "unknown"
        call_id = tool_call_id if isinstance(tool_call_id, str) else ""
        return f"[tool_result {name} {call_id}] {text}"
    return _content_body(message.get("content"))


def _content_body(content: JsonValue) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_body(block))
    return "\n".join(parts)


def _tool_call_body(block: dict[str, JsonValue]) -> str:
    name = block.get("name")
    call_id = block.get("id")
    arguments = block.get("arguments")
    name_text = name if isinstance(name, str) else ""
    id_text = call_id if isinstance(call_id, str) else ""
    arguments_json = json.dumps(
        {} if arguments is None else arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"[tool_call {name_text} {id_text}] {arguments_json}"


def _record_digest(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _checkpoint_events(checkpoint: JsonValue | None, session_id: str) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict) or checkpoint.get("version") != _CHECKPOINT_VERSION:
        raise ValueError("Pi session checkpoint is not valid")  # noqa: TRY003
    if checkpoint.get("session_id") != session_id:
        return {}
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint is not valid")  # noqa: TRY003, TRY004
    imported: dict[str, str] = {}
    for event_id, digest in events.items():
        if not isinstance(event_id, str) or not isinstance(digest, str):
            raise ValueError("Pi session checkpoint is not valid")  # noqa: TRY003, TRY004
        imported[event_id] = digest
    return imported


__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_EVENT_SOURCE_VERSION",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PI_SESSION_FORMAT_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
