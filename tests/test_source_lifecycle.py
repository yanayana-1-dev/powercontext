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

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.records import ArtifactWrite
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    RememberMemoryRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.source_lifecycle import (
    SourceLifecycleError,
    SourceLifecycleEvent,
)
from powercontext.builtin.scope import ScopeDraft


def _event(node: str, sequence: int, available: bool, revision: int = 2) -> SourceLifecycleEvent:
    return SourceLifecycleEvent(
        node_id=node,
        revision=revision,
        sequence=sequence,
        available=available,
        reason="controlled lifecycle",
    )


def test_event_fields_are_strict() -> None:
    event = _event("root", 1, False)
    assert event.node_id == "root" and event.available is False
    for kwargs in (
        {"node_id": "x", "revision": 0, "sequence": 1, "available": False, "reason": "r"},
        {"node_id": "x", "revision": 1, "sequence": 0, "available": False, "reason": "r"},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": False, "reason": ""},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": False, "reason": "   "},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": 1, "reason": "r"},
        {"node_id": "x", "revision": 1, "sequence": 1, "available": False, "reason": "r", "extra": True},
    ):
        with pytest.raises(ValidationError):
            SourceLifecycleEvent(**kwargs)
    assert issubclass(SourceLifecycleError, ValueError)


async def _scope(runtime, key: str) -> str:
    scope = await runtime.scopes.create(
        ScopeDraft(title="Lifecycle", summary="Dependency evidence", idempotency_key=key)
    )
    return scope.scope_id


async def _source(runtime, scope: str, node: str):
    receipt = await runtime.sources.for_scope(scope).capture(
        CaptureSource(source_id=node, content=f"Origin {node}", metadata={})
    )
    return receipt.source_ref


async def _memory(runtime, scope: str, node: str):
    result = await runtime.memory.for_scope(scope).remember(
        RememberMemoryRequest(entries=(MemoryEntryInput(kind="fact", text=f"{node} fixture evidence"),))
    )
    return result.entry.citation


async def _topic(runtime, scope: str, node: str) -> ArtifactRef:
    created = await runtime.records.for_scope(scope).create_artifact(
        "topic-memory",
        ArtifactWrite(content={"title": node, "summary": f"{node} summary", "detail": f"{node} detail"}),
    )
    return ArtifactRef(family="topic-memory", artifact_id=created.artifact_id, revision=created.revision)


def test_dag_all_any_generation_and_audit() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope = await _scope(runtime, "dag")
            service = runtime.lifecycle.for_scope(scope)
            root_a = await _source(runtime, scope, "a")
            root_b = await _source(runtime, scope, "b")
            await service.register_source("a", root_a, revision=2)
            await service.register_source("b", root_b, revision=2)
            memory_all = await _memory(runtime, scope, "all")
            memory_any = await _memory(runtime, scope, "any")
            await service.register_memory("all", memory_all, parents=("a", "b"), policy="all")
            await service.register_memory("any", memory_any, parents=("a", "b"), policy="any")
            topic = await _topic(runtime, scope, "topic")
            await service.register_topic("topic", topic, parents=("all",), policy="all")

            active = await service.apply(_event("a", 1, False))
            assert active == ["any", "b"]
            with pytest.raises(SourceLifecycleError):
                await service.resolve("all", purpose="generation")
            assert await service.resolve("all", purpose="audit") == memory_all
            assert await service.resolve("any", purpose="generation") == memory_any
            with pytest.raises(SourceLifecycleError):
                await service.resolve("topic", purpose="generation")

            active = await service.apply(_event("a", 2, True))
            assert active == ["a", "all", "any", "b", "topic"]
            assert await service.resolve("topic", purpose="generation") == topic

    asyncio.run(scenario())


def test_registration_rejects_invalid_graph_and_references() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope = await _scope(runtime, "invalid")
            service = runtime.lifecycle.for_scope(scope)
            root = await _source(runtime, scope, "root")
            citation = await _memory(runtime, scope, "needle")
            await service.register_source("root", root, revision=2)
            await service.register_memory("needle", citation, parents=("root",), policy="all")
            for parents in ((), ("missing",), ("root", "root")):
                with pytest.raises(SourceLifecycleError):
                    await service.register_memory("next", citation, parents=parents, policy="all")
            with pytest.raises(SourceLifecycleError):
                await service.register_memory("self", citation, parents=("self",), policy="all")
            with pytest.raises(SourceLifecycleError):
                await service.register_memory("needle", citation, parents=("root",), policy="any")
            with pytest.raises(SourceLifecycleError):
                await service.register_source("ghost", type(root)(source_type="content", source_id="ghost"), revision=2)

    asyncio.run(scenario())


def test_event_version_sequence_and_idempotent_replay() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(database=SQLiteConfig())) as runtime:
            scope = await _scope(runtime, "events")
            service = runtime.lifecycle.for_scope(scope)
            root = await _source(runtime, scope, "root")
            citation = await _memory(runtime, scope, "needle")
            await service.register_source("root", root, revision=2)
            await service.register_memory("needle", citation, parents=("root",), policy="all")
            active = await service.apply(_event("root", 2, False))
            assert active == []
            before = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert await service.apply(_event("root", 2, False)) == active
            after = (await runtime.memory.for_scope(scope).list(include_inactive=True)).memory_ref
            assert before == after
            for invalid in (
                _event("root", 1, True),
                _event("root", 2, True),
                _event("root", 3, True, revision=1),
                _event("unknown", 1, True),
            ):
                with pytest.raises(SourceLifecycleError):
                    await service.apply(invalid)
            assert await service.apply(_event("root", 3, True)) == ["needle", "root"]

    asyncio.run(scenario())


def test_scope_isolation_and_restart(tmp_path) -> None:
    async def scenario() -> None:
        database = SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'lifecycle.db'}")
        async with open_builtin_runtime(BuiltinConfig(database=database)) as runtime:
            first = await _scope(runtime, "first")
            second = await _scope(runtime, "second")
            for scope in (first, second):
                service = runtime.lifecycle.for_scope(scope)
                root = await _source(runtime, scope, "root")
                await service.register_source("root", root, revision=1)
            await runtime.lifecycle.for_scope(first).apply(_event("root", 1, False, revision=1))
            assert await runtime.lifecycle.for_scope(first).resolve("root", purpose="audit")
            with pytest.raises(SourceLifecycleError):
                await runtime.lifecycle.for_scope(first).resolve("root", purpose="generation")
            assert await runtime.lifecycle.for_scope(second).resolve("root", purpose="generation")
        async with open_builtin_runtime(BuiltinConfig(database=database)) as runtime:
            first_service = runtime.lifecycle.for_scope(first)
            with pytest.raises(SourceLifecycleError):
                await first_service.resolve("root", purpose="generation")
            assert await first_service.resolve("root", purpose="audit")
            assert await runtime.lifecycle.for_scope(second).resolve("root", purpose="generation")

    asyncio.run(scenario())
