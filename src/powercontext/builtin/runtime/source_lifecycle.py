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

"""Source withdrawal and restore propagation across Memory and Topic Memory."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.limits import MAX_SOURCE_ID_LENGTH, MAX_SCOPE_ID_LENGTH
from powercontext.sources import SourceRef

LifecycleKind = Literal["source", "memory", "topic"]
LifecyclePolicy = Literal["all", "any"]


class SourceLifecycleError(ValueError):
    """A Source lifecycle registration, event, or resolution violation."""


class SourceLifecycleEvent(BaseModel):
    """One durable availability transition for one registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str
    revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    available: bool
    reason: str

    @field_validator("node_id", "reason")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise SourceLifecycleError("node_id and reason must contain non-whitespace content")
        return value


def _event_hash(event: SourceLifecycleEvent, /) -> str:
    import hashlib

    payload = json.dumps(
        {
            "node_id": event.node_id,
            "revision": event.revision,
            "sequence": event.sequence,
            "available": event.available,
            "reason": event.reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_payload(value: object) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return json.loads(bytes(value).decode("utf-8"))
    if isinstance(value, str):
        return json.loads(value)
    return value


class _NodeRow(BaseModel):
    """One persisted lifecycle node."""

    node_id: str
    kind: LifecycleKind
    reference: dict[str, Any]
    parents: tuple[str, ...]
    policy: LifecyclePolicy
    revision: int
    sequence: int
    available: bool
    event_hash: str | None = None

    def reference_json(self) -> str:
        return json.dumps(self.reference, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


async def _load_nodes(connection: AsyncConnection, scope_id: str) -> dict[str, _NodeRow]:
    rows = (
        await connection.execute(
            select(SOURCE_LIFECYCLE_NODES_TABLE).where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id)
        )
    ).mappings()
    nodes: dict[str, _NodeRow] = {}
    for row in rows:
        parents = tuple(str(item) for item in _decode_payload(row["parents"]))
        nodes[str(row["node_id"])] = _NodeRow(
            node_id=str(row["node_id"]),
            kind=row["kind"],
            reference=_decode_payload(row["reference"]),
            parents=parents,
            policy=row["policy"],
            revision=int(row["revision"]),
            sequence=int(row["sequence"]),
            available=bool(row["available"]),
            event_hash=None if row["event_hash"] is None else str(row["event_hash"]),
        )
    return nodes


async def _save_node(connection: AsyncConnection, scope_id: str, node: _NodeRow) -> None:
    await connection.execute(
        update(SOURCE_LIFECYCLE_NODES_TABLE)
        .where(
            SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == scope_id,
            SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node.node_id,
        )
        .values(
            kind=node.kind,
            reference=node.reference_json().encode("utf-8"),
            parents=json.dumps(list(node.parents)).encode("utf-8"),
            policy=node.policy,
            revision=node.revision,
            sequence=node.sequence,
            available=node.available,
            event_hash=node.event_hash,
        )
    )


async def _insert_node(connection: AsyncConnection, scope_id: str, node: _NodeRow) -> None:
    await connection.execute(
        SOURCE_LIFECYCLE_NODES_TABLE.insert().values(
            scope_id=scope_id,
            node_id=node.node_id,
            kind=node.kind,
            reference=node.reference_json().encode("utf-8"),
            parents=json.dumps(list(node.parents)).encode("utf-8"),
            policy=node.policy,
            revision=node.revision,
            sequence=node.sequence,
            available=node.available,
            event_hash=node.event_hash,
        )
    )


def compute_active(nodes: dict[str, _NodeRow]) -> dict[str, bool]:
    """Evaluate availability over the dependency DAG until it settles."""

    active: dict[str, bool] = {}
    pending = set(nodes)
    while pending:
        progressed = False
        for node_id in sorted(pending):
            node = nodes[node_id]
            if node.kind == "source":
                active[node_id] = node.available
                pending.discard(node_id)
                progressed = True
                continue
            if any(parent not in active for parent in node.parents):
                continue
            support = [active[parent] for parent in node.parents]
            active[node_id] = all(support) if node.policy == "all" else any(support)
            pending.discard(node_id)
            progressed = True
        if not progressed:
            raise SourceLifecycleError("dependency cycle")
    return active


class ScopedSourceLifecycleService:
    """Register evidence dependencies and propagate Source availability in one scope."""

    def __init__(self, runtime: Any, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = scope_id

    async def register_source(self, node_id: str, reference: SourceRef, /, *, revision: int = 1) -> None:
        await self._register(node_id, "source", reference.model_dump(mode="json"), (), "all", revision)

    async def register_memory(
        self,
        node_id: str,
        reference: Any,
        /,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy,
    ) -> None:
        from powercontext.builtin.artifacts.memory.models import MemoryCitation

        if not isinstance(reference, MemoryCitation):
            raise SourceLifecycleError("memory nodes require a MemoryCitation")
        await self._register(node_id, "memory", reference.model_dump(mode="json"), parents, policy, 1)

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: LifecyclePolicy,
    ) -> None:
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("topic nodes require an ArtifactRef")
        await self._register(node_id, "topic", reference.model_dump(mode="json"), parents, policy, 1)

    async def _register(
        self,
        node_id: str,
        kind: LifecycleKind,
        reference: dict[str, Any],
        parents: tuple[str, ...],
        policy: LifecyclePolicy,
        revision: int,
    ) -> None:
        _require_node_id(node_id)
        if policy not in {"all", "any"}:
            raise SourceLifecycleError("policy must be all or any")
        if kind != "source" and not parents:
            raise SourceLifecycleError("derived nodes require at least one parent")
        if node_id in parents:
            raise SourceLifecycleError("dependency cycles are not allowed")
        await self._verify_reference(kind, reference)
        database = self._database()
        async with database.transaction() as connection:
            nodes = await _load_nodes(connection, self.scope_id)
            existing = nodes.get(node_id)
            binding = (kind, reference, parents, policy)
            if existing is not None:
                current = (existing.kind, existing.reference, existing.parents, existing.policy)
                if current != binding:
                    raise SourceLifecycleError(f"node {node_id!r} is already bound differently")
                return
            for parent in parents:
                if parent not in nodes:
                    raise SourceLifecycleError(f"parent {parent!r} is not registered")
            if _has_cycle(node_id, parents, nodes):
                raise SourceLifecycleError("dependency cycles are not allowed")
            nodes[node_id] = _NodeRow(
                node_id=node_id,
                kind=kind,
                reference=reference,
                parents=parents,
                policy=policy,
                revision=revision,
                sequence=0,
                available=kind == "source",
            )
            active = compute_active(nodes)
            node = nodes[node_id]
            node.available = active[node_id]
            await _insert_node(connection, self.scope_id, node)
        await self._sync_native(nodes, active)

    async def _verify_reference(self, kind: str, reference: dict[str, Any]) -> None:
        runtime = self._runtime
        scope_id = self.scope_id
        try:
            if kind == "source":
                ref = SourceRef.model_validate(reference)
                await runtime.records.for_scope(scope_id).get_source(ref.source_type, ref.source_id)
            elif kind == "memory":
                from powercontext.builtin.artifacts.memory.models import MemoryCitation
                from powercontext.builtin.runtime import GetMemoryEntryRequest

                citation = MemoryCitation.model_validate(reference)
                await runtime.memory.for_scope(scope_id).get(GetMemoryEntryRequest(citation=citation))
            else:
                from powercontext.builtin.records import ArtifactRef as _ArtifactRef

                artifact = _ArtifactRef.model_validate(reference)
                await runtime.records.for_scope(scope_id).get_artifact_revision(
                    artifact.family, artifact.artifact_id, artifact.revision
                )
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError(f"registered reference is not readable: {error}") from error

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Record one transition, propagate availability, and sync native state."""

        database = self._database()
        async with database.transaction() as connection:
            nodes = await _load_nodes(connection, self.scope_id)
            node = nodes.get(event.node_id)
            if node is None or node.kind != "source":
                raise SourceLifecycleError(f"source {event.node_id!r} is not registered")
            if event.revision != node.revision:
                raise SourceLifecycleError(f"revision {event.revision} does not match {node.revision}")
            if event.sequence < node.sequence:
                raise SourceLifecycleError("sequence must be monotonically increasing")
            if event.sequence == node.sequence:
                if node.event_hash != _event_hash(event):
                    raise SourceLifecycleError("sequence was already used with a different event")
                # Idempotent replay: the durable event is already recorded. Native
                # state is re-synchronized below without recording a new event.
            else:
                node.available = event.available
                node.sequence = event.sequence
                node.event_hash = _event_hash(event)
                await _save_node(connection, self.scope_id, node)
            active = compute_active(nodes)
            for node_id, value in active.items():
                if nodes[node_id].available != value:
                    nodes[node_id].available = value
                    await _save_node(connection, self.scope_id, nodes[node_id])
        await self._sync_native(nodes, active)
        return sorted(node_id for node_id, value in active.items() if value)

    async def _sync_native(self, nodes: dict[str, _NodeRow], active: dict[str, bool]) -> None:
        """Bring native Memory entry states in line with the propagated availability."""

        from powercontext.builtin.runtime.application import _head_or_none
        from powercontext.builtin.statistics import ModelUsagePurpose

        runtime = self._runtime
        scope_id = self.scope_id
        async with runtime._context(scope_id, embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING) as context:
            service = context.artifacts.memory
            current = await _head_or_none(service, context.artifacts.memory_artifact_id)
            if current is None:
                return
            states = {item.entry_id: item.state for item in current.content.manifest.entries}
            pending: list[tuple[str, bool]] = []
            for node_id, node in nodes.items():
                if node.kind != "memory":
                    continue
                entry_id = str(node.reference.get("entry_id", ""))
                state = states.get(entry_id)
                if state is None:
                    continue
                target = bool(active[node_id])
                if (state == "active") != target:
                    pending.append((entry_id, target))
            if not pending:
                return
            async with runtime._locked(scope_id):
                for entry_id, target_active in pending:
                    states = {item.entry_id: item.state for item in current.content.manifest.entries}
                    state = states.get(entry_id)
                    if state is None or (state == "active") == target_active:
                        continue
                    entry = next(item for item in await service.entries(current) if item.entry_id == entry_id)
                    if target_active:
                        current = await service.reactivate(current, entries=(entry,), reason="source-lifecycle")
                    else:
                        current = await service.forget(current, entries=(entry,), reason="source-lifecycle")

    async def resolve(self, node_id: str, /, *, purpose: str = "generation") -> Any:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError(f"unknown resolve purpose {purpose!r}")
        database = self._database()
        async with database.transaction() as connection:
            nodes = await _load_nodes(connection, self.scope_id)
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"node {node_id!r} is not registered")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"node {node_id!r} is not available for generation")
        return _rebuild_reference(node.kind, node.reference)

    def _database(self) -> Any:
        database = getattr(self._runtime, "_lifecycle_database", None)
        if database is None:
            raise SourceLifecycleError("source lifecycle persistence is not configured")
        return database


def _has_cycle(node_id: str, parents: tuple[str, ...], nodes: dict[str, _NodeRow]) -> bool:
    seen: set[str] = set()

    def visit(current: str) -> bool:
        if current == node_id:
            return True
        if current in seen:
            return False
        seen.add(current)
        node = nodes.get(current)
        return node is not None and any(visit(parent) for parent in node.parents)

    return any(visit(parent) for parent in parents)


def _rebuild_reference(kind: str, reference: dict[str, Any]) -> Any:
    if kind == "source":
        return SourceRef.model_validate(reference)
    if kind == "memory":
        from powercontext.builtin.artifacts.memory.models import MemoryCitation

        return MemoryCitation.model_validate(reference)
    return ArtifactRef.model_validate(reference)


def _require_node_id(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SourceLifecycleError("node_id must contain non-whitespace content")
    if len(value) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError(f"node_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    if len(value) > MAX_SCOPE_ID_LENGTH:  # pragma: no cover - same bound class
        raise SourceLifecycleError("node_id is too long")


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle service."""

    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        from powercontext.builtin.runtime.application import validate_scope_id

        return ScopedSourceLifecycleService(self._runtime, validate_scope_id(scope_id))


__all__ = [
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "ScopedSourceLifecycleService",
]
