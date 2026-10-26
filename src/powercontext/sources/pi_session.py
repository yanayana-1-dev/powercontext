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

"""Selected-branch Pi session history capture with incremental checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
_PI_SESSION_CHECKPOINT_VERSION = 1


class PiSessionInput(BaseModel):
    """One complete offline Pi session archive awaiting branch extraction."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event on the selected session branch."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict


class PiEventSource(Source):
    """One captured Pi session event preserved as exact original evidence."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict


def _parse_records(content: bytes) -> tuple[str, list[dict]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"session must be valid UTF-8: {error}") from error
    records: list[dict] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed session JSONL: {error}") from error
        if not isinstance(record, dict):
            raise ValueError("session records must be JSON objects")  # noqa: TRY003
        records.append(record)
    if not records:
        raise ValueError("session archive must contain a header record")  # noqa: TRY003
    header = records[0]
    if header.get("type") != "session" or header.get("version") != 3:
        raise ValueError("first record must declare type=session and version=3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session header id must be a non-empty string")  # noqa: TRY003
    return session_id, records[1:]


def _selected_chain(records: list[dict], leaf_id: str | None) -> list[dict]:
    by_id: dict[str, dict] = {}
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("every session record must carry a non-empty id")  # noqa: TRY003
        if event_id in by_id:
            raise ValueError(f"duplicate session record id {event_id!r}")  # noqa: TRY003
        parent_id = record.get("parentId")
        if parent_id in ("", None):
            parent_id = None
        elif not isinstance(parent_id, str) or parent_id not in by_id:
            raise ValueError(f"record {event_id!r} references a missing or forward parent")  # noqa: TRY003
        by_id[event_id] = record
    if not by_id:
        return []
    if leaf_id is None:
        leaf_id = records[-1]["id"]
    if leaf_id not in by_id:
        raise ValueError(f"leaf {leaf_id!r} is not present in the session graph")  # noqa: TRY003
    chain: list[dict] = []
    cursor: str | None = leaf_id
    while cursor is not None:
        record = by_id[cursor]
        chain.append(record)
        parent_id = record.get("parentId")
        cursor = None if parent_id in ("", None) else parent_id
    chain.reverse()
    return chain


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _visible_parts(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    parts: list[str] = []
    if isinstance(value, list):
        for block in value:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block_type == "toolCall":
                arguments = block.get("arguments")
                if not isinstance(arguments, (dict, list, str, int, float, bool)) or arguments is None:
                    arguments = {}
                parts.append(
                    f"[tool_call {block.get('name', '')} {block.get('id', '')}] {_compact_json(arguments)}"
                )
    return parts


def _event_body(record: dict) -> tuple[str, str] | None:
    """Return the visible ``(role, body)`` of one record or ``None`` for control nodes."""

    event_type = record.get("type")
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        if role == "toolResult":
            parts = _visible_parts(message.get("content"))
            prefix = f"[tool_result {message.get('toolName', '')} {message.get('toolCallId', '')}]"
            body = prefix if not parts else prefix + " " + "\n".join(parts)
            return role, body
        if role == "bashExecution":
            command = message.get("command")
            output = message.get("output")
            if not isinstance(command, str):
                return None
            return role, f"$ {command}\n{output if isinstance(output, str) else ''}"
        parts = _visible_parts(message.get("content"))
        if not parts:
            return None
        return role, "\n".join(parts)
    if event_type in ("compaction", "branch_summary"):
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None
        return event_type, summary
    if event_type == "custom_message":
        parts = _visible_parts(record.get("content"))
        if not parts:
            return None
        return "custom", "\n".join(parts)
    return None


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Parse the archive and return the selected branch's visible events root-first."""

    session_id, records = _parse_records(value.content)
    events: list[PiEventInput] = []
    for record in _selected_chain(records, value.leaf_id):
        body = _event_body(record)
        if body is None:
            continue
        role, text = body
        parent_id = record.get("parentId")
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=record["id"],
                parent_id=None if parent_id in ("", None) else parent_id,
                event_type=record.get("type", ""),
                role=role,
                content=f"{role}: {text}",
                record=record,
            )
        )
    return tuple(events)


def pi_event_source_id(session_id: str, event_id: str, /) -> str:
    """Build the stable identity of one captured session event."""

    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi event identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


class PiEventSourceAdapter:
    """Resolve and read Pi session event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        identity = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
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


class PiEventTextEvidenceProjection:
    """Expose one event as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

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


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    projections=(PiEventTextEvidenceProjection(),),
)


def _record_digest(record: dict) -> str:
    return hashlib.sha256(_compact_json(record).encode("utf-8")).hexdigest()


class PiSessionConnector:
    """Incrementally import the selected branch with a durable per-file checkpoint."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: Path | str, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        imported: dict[str, str] = {}
        checkpoint = session.checkpoint
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("records"), dict):
            imported = {
                str(event_id): str(digest)
                for event_id, digest in checkpoint["records"].items()
                if isinstance(digest, str)
            }
        content = self._path.read_bytes()
        session_id, records = _parse_records(content)
        for record in records:
            event_id = record.get("id")
            if not isinstance(event_id, str):
                continue
            expected = imported.get(event_id)
            if expected is not None and expected != _record_digest(record):
                raise ValueError(f"imported session event {event_id!r} was rewritten")  # noqa: TRY003
        updated = dict(imported)
        for event in parse_pi_session(PiSessionInput(content=content, leaf_id=self._leaf_id)):
            if event.session_id != session_id:  # pragma: no cover - same archive
                continue
            if event.event_id in imported:
                continue
            await session.submit(pi_event_source_id(event.session_id, event.event_id), PI_EVENT_SOURCE_NAME, event)
            updated[event.event_id] = _record_digest(event.record)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": _PI_SESSION_CHECKPOINT_VERSION, "records": updated},
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
