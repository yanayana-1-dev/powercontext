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

# ruff: noqa: TRY003, TRY301

"""Source-driven lifecycles for Memory and Topic Memory eligibility.

Callers register the evidence dependency DAG explicitly. Withdrawing or
restoring a Source propagates ``all``/``any`` support to every descendant and
keeps native Memory states and Topic search eligibility in sync while retaining
the original references for audit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.artifacts.models import MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

Purpose = Literal["generation", "audit"]
Kind = Literal["source", "memory", "topic"]
Policy = Literal["all", "any"]


class SourceLifecycleError(ValueError):
    """A lifecycle graph, event, or reference violates the registered contract."""


class SourceLifecycleEvent(BaseModel):
    """One validated Source availability event."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    node_id: str = Field(min_length=1)
    revision: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=1)
    available: StrictBool
    reason: str = Field(min_length=1)

    @field_validator("node_id", "reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


@dataclass(slots=True)
class _Node:
    node_id: str
    kind: Kind
    reference: dict[str, Any]
    parents: tuple[str, ...]
    policy: Policy
    revision: int
    available: bool
    sequence: int
    last_available: bool | None
    last_reason: str | None
    ordinal: int
    artifact_id: str | None
    artifact_revision: int | None


class SourceLifecycleApplication:
    """Select a scope-bound lifecycle service."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> SourceLifecycleService:
        return SourceLifecycleService(self._runtime, scope_id)


class SourceLifecycleService:
    """Register one evidence DAG and propagate Source availability through it."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(self, node_id: str, reference: SourceRef, *, revision: int) -> None:
        self._require_node_id(node_id)
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(reference, SourceRef):
            raise SourceLifecycleError("source reference must be a SourceRef")
        await self._register("source", node_id, reference, parents=(), policy="all", revision=revision)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: tuple[str, ...],
        policy: Policy = "all",
    ) -> None:
        self._require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory reference must be a MemoryCitation")
        await self._register("memory", node_id, citation, parents=parents, policy=policy, revision=1)

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: Policy = "all",
    ) -> None:
        self._require_node_id(node_id)
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("topic reference must be an ArtifactRef")
        await self._register("topic", node_id, reference, parents=parents, policy=policy, revision=1)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            states = await self._persist_event(event)
            await self._reconcile_memory(states)
            return sorted(node_id for node_id, available in states.items() if available)

    async def resolve(self, node_id: str, *, purpose: Purpose) -> SourceRef | MemoryCitation | ArtifactRef:
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("unknown purpose")
        async with self._database().transaction() as connection:
            nodes = await self._load(connection)
            node = nodes.get(node_id)
            if node is None:
                raise SourceLifecycleError(f"unknown node: {node_id}")
            if purpose == "generation" and not node.available:
                raise SourceLifecycleError(f"node is not available: {node_id}")
            return self._reference(node)

    async def _register(
        self,
        kind: Kind,
        node_id: str,
        reference: SourceRef | MemoryCitation | ArtifactRef,
        *,
        parents: tuple[str, ...],
        policy: Policy,
        revision: int,
    ) -> None:
        if policy not in {"all", "any"}:
            raise SourceLifecycleError("policy must be 'all' or 'any'")
        parent_ids = () if kind == "source" else self._validate_parent_ids(node_id, parents)
        await self._validate_reference(kind, reference)
        payload = reference.model_dump(mode="json")
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            async with self._database().transaction() as connection:
                nodes = await self._load(connection)
                existing = nodes.get(node_id)
                if existing is not None:
                    if not self._same_binding(existing, kind, payload, parent_ids, policy, revision):
                        raise SourceLifecycleError(f"node already registered with a different binding: {node_id}")
                    node = existing
                else:
                    for parent in parent_ids:
                        if parent not in nodes:
                            raise SourceLifecycleError(f"unknown parent: {parent}")
                    ordinal = max((item.ordinal for item in nodes.values()), default=0) + 1
                    available = self._derive_availability(parent_ids, policy, nodes) if kind != "source" else True
                    artifact_id = payload.get("artifact_id") if kind == "topic" else None
                    artifact_revision = payload.get("revision") if kind == "topic" else None
                    await connection.execute(
                        insert(SOURCE_LIFECYCLE_TABLE).values(
                            scope_id=self.scope_id,
                            node_id=node_id,
                            kind=kind,
                            reference=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                            parents=json.dumps(list(parent_ids), separators=(",", ":")),
                            policy=policy,
                            revision=revision,
                            available=available,
                            sequence=0,
                            last_available=None,
                            last_reason=None,
                            ordinal=ordinal,
                            artifact_id=artifact_id,
                            artifact_revision=artifact_revision,
                        )
                    )
                    node = _Node(
                        node_id=node_id,
                        kind=kind,
                        reference=payload,
                        parents=parent_ids,
                        policy=policy,
                        revision=revision,
                        available=available,
                        sequence=0,
                        last_available=None,
                        last_reason=None,
                        ordinal=ordinal,
                        artifact_id=artifact_id,
                        artifact_revision=artifact_revision,
                    )
            if node.kind == "memory":
                await self._reconcile_memory({node.node_id: node.available})

    async def _persist_event(self, event: SourceLifecycleEvent) -> dict[str, bool]:
        async with self._database().transaction() as connection:
            nodes = await self._load(connection)
            source = nodes.get(event.node_id)
            if source is None or source.kind != "source":
                raise SourceLifecycleError(f"unknown Source: {event.node_id}")
            if event.revision != source.revision:
                raise SourceLifecycleError("event revision does not match the Source lifecycle version")
            if event.sequence < source.sequence:
                raise SourceLifecycleError("event sequence is lower than the stored sequence")
            idempotent = event.sequence == source.sequence
            if idempotent:
                if event.available != source.last_available or event.reason != source.last_reason:
                    raise SourceLifecycleError("event sequence was already used by different content")
            else:
                source.available = event.available
                source.sequence = event.sequence
                source.last_available = event.available
                source.last_reason = event.reason
                await connection.execute(
                    update(SOURCE_LIFECYCLE_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id,
                        SOURCE_LIFECYCLE_TABLE.c.node_id == source.node_id,
                    )
                    .values(
                        available=source.available,
                        sequence=source.sequence,
                        last_available=source.last_available,
                        last_reason=source.last_reason,
                    )
                )
            states = self._derive_states(nodes)
            for node in nodes.values():
                if node.kind == "source":
                    continue
                if node.available != states[node.node_id]:
                    node.available = states[node.node_id]
                    await connection.execute(
                        update(SOURCE_LIFECYCLE_TABLE)
                        .where(
                            SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id,
                            SOURCE_LIFECYCLE_TABLE.c.node_id == node.node_id,
                        )
                        .values(available=node.available)
                    )
            return states

    def _derive_states(self, nodes: dict[str, _Node]) -> dict[str, bool]:
        states: dict[str, bool] = {}
        for node in sorted(nodes.values(), key=lambda item: item.ordinal):
            if node.kind == "source":
                states[node.node_id] = node.available
            else:
                states[node.node_id] = self._derive_availability(node.parents, node.policy, nodes, states)
        return states

    @staticmethod
    def _derive_availability(
        parents: tuple[str, ...],
        policy: Policy,
        nodes: dict[str, _Node],
        states: dict[str, bool] | None = None,
    ) -> bool:
        values = [states[parent] if states is not None else nodes[parent].available for parent in parents]
        return all(values) if policy == "all" else any(values)

    async def _reconcile_memory(self, states: dict[str, bool]) -> None:
        memory_nodes = [node for node in await self._load_all() if node.kind == "memory"]
        if not memory_nodes:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            for node in memory_nodes:
                await self._set_memory_state(service, node, states.get(node.node_id, node.available))

    async def _set_memory_state(self, service: Any, node: _Node, available: bool) -> None:
        citation = MemoryCitation.model_validate(node.reference)
        try:
            memory = await service.head(citation.memory_ref.artifact_id)
        except Exception as error:
            raise SourceLifecycleError("registered Memory reference is not readable") from error
        manifest = next(
            (
                item
                for item in memory.content.manifest.entries
                if item.entry_id == citation.entry_id and item.entry_version_id == citation.entry_version_id
            ),
            None,
        )
        if manifest is None:
            raise SourceLifecycleError("registered Memory entry is missing from the current Memory")
        desired = "active" if available else "inactive"
        if str(manifest.state) == desired:
            return
        try:
            entry = await service.validate_citation(citation)
            if available:
                await service.reactivate(memory, entries=(entry,), reason="source lifecycle")
            else:
                await service.forget(memory, entries=(entry,), reason="source lifecycle")
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError("registered Memory state could not be updated") from error

    async def _validate_reference(
        self,
        kind: Kind,
        reference: SourceRef | MemoryCitation | ArtifactRef,
    ) -> None:
        try:
            if kind == "source":
                if not isinstance(reference, SourceRef):
                    raise SourceLifecycleError("source reference must be a SourceRef")
                await self._runtime._records().get_source(
                    self.scope_id,
                    reference.source_type,
                    reference.source_id,
                )
            elif kind == "memory":
                if not isinstance(reference, MemoryCitation):
                    raise SourceLifecycleError("memory reference must be a MemoryCitation")
                async with self._runtime._context(self.scope_id) as context:
                    service = context.artifacts.memory
                    memory = await service.revision(reference.memory_ref)
                    if not any(
                        item.entry_id == reference.entry_id and item.entry_version_id == reference.entry_version_id
                        for item in memory.content.manifest.entries
                    ):
                        raise SourceLifecycleError("Memory citation does not identify a readable entry")
            else:
                if not isinstance(reference, ArtifactRef):
                    raise SourceLifecycleError("topic reference must be an ArtifactRef")
                if reference.family != "topic-memory":
                    raise SourceLifecycleError("topic reference must belong to topic-memory")
                await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                    "topic-memory",
                    reference.artifact_id,
                    reference.revision,
                )
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError(f"registered {kind} reference is not readable") from error

    async def _load_all(self) -> list[_Node]:
        async with self._database().transaction() as connection:
            nodes = await self._load(connection)
            return list(nodes.values())

    async def _load(self, connection: AsyncConnection) -> dict[str, _Node]:
        rows = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id)
            )
        ).mappings()
        return {
            str(row["node_id"]): _Node(
                node_id=str(row["node_id"]),
                kind=row["kind"],
                reference=json.loads(str(row["reference"])),
                parents=tuple(str(parent) for parent in json.loads(str(row["parents"]))),
                policy=row["policy"],
                revision=int(row["revision"]),
                available=bool(row["available"]),
                sequence=int(row["sequence"]),
                last_available=None if row["last_available"] is None else bool(row["last_available"]),
                last_reason=None if row["last_reason"] is None else str(row["last_reason"]),
                ordinal=int(row["ordinal"]),
                artifact_id=None if row["artifact_id"] is None else str(row["artifact_id"]),
                artifact_revision=None if row["artifact_revision"] is None else int(row["artifact_revision"]),
            )
            for row in rows
        }

    def _database(self) -> Any:
        return self._runtime._records()._database

    @staticmethod
    def _require_node_id(node_id: str) -> None:
        if not isinstance(node_id, str) or not node_id.strip():
            raise SourceLifecycleError("node_id must be a non-empty string")

    @staticmethod
    def _validate_parent_ids(node_id: str, parents: tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(parents, (tuple, list)) or not parents:
            raise SourceLifecycleError("a derived node requires at least one parent")
        parent_ids = tuple(parents)
        if len(set(parent_ids)) != len(parent_ids):
            raise SourceLifecycleError("parents must be unique")
        if node_id in parent_ids:
            raise SourceLifecycleError("a node cannot depend on itself")
        for parent in parent_ids:
            if not isinstance(parent, str) or not parent.strip():
                raise SourceLifecycleError("parents must be non-empty node ids")
        return parent_ids

    @staticmethod
    def _same_binding(
        node: _Node,
        kind: Kind,
        reference: dict[str, Any],
        parents: tuple[str, ...],
        policy: Policy,
        revision: int,
    ) -> bool:
        return (
            node.kind == kind
            and node.reference == reference
            and node.parents == parents
            and node.policy == policy
            and node.revision == revision
        )

    @staticmethod
    def _reference(node: _Node) -> SourceRef | MemoryCitation | ArtifactRef:
        if node.kind == "source":
            return SourceRef.model_validate(node.reference)
        if node.kind == "memory":
            return MemoryCitation.model_validate(node.reference)
        return ArtifactRef.model_validate(node.reference)


__all__ = [
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
    "SourceLifecycleService",
]
