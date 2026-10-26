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
import base64
import hashlib
import json

import pytest

from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
from powercontext.builtin.runtime import (
    BuiltinConfig,
    CaptureSource,
    PrepareContextRequest,
    open_builtin_runtime,
)
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    ContextPackError,
    apply_context_pack_delta,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.builtin.sources import ContentSource


def test_wire_shape_digest_and_byte_count_are_canonical() -> None:
    items = (b"alpha\n", "海洋".encode())
    unsigned = {"schema": "powercontext.context-pack.v1", "items": [base64.b64encode(item).decode() for item in items]}
    pack = ContextPack.build(items)
    payload = json.loads(pack.to_bytes())

    assert payload == {
        **unsigned,
        "digest": "sha256:"
        + hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "byte_count": len(pack.to_bytes()),
    }
    assert pack.to_bytes() == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert pack.digest != ContextPack.build((b"alpha",)).digest


@pytest.mark.parametrize(("content", "limit"), [("alpha 海洋 beta", 7), (b"alpha \xe6\xb5\xb7\xe6\xb4\x8b beta", 7)])
def test_chunking_is_bounded_valid_and_lossless(content, limit: int) -> None:
    pack = ContextPack.from_content(content, chunk_bytes=limit)
    original = content.encode() if isinstance(content, str) else content
    assert b"".join(pack.items) == original
    assert all(len(item) <= limit and item.decode("utf-8") is not None for item in pack.items)
    assert ContextPack.from_content(content, chunk_bytes=limit).items == pack.items


def test_chunking_rejects_impossible_boundaries_and_bad_limits() -> None:
    with pytest.raises(ContextPackError):
        ContextPack.from_content("海", chunk_bytes=2)
    for limit in (0, -1, True, 1.5):
        with pytest.raises(ContextPackError):
            ContextPack.from_content("abc", chunk_bytes=limit)  # type: ignore[arg-type]
    with pytest.raises(ContextPackError):
        ContextPack.from_content(b"\xff", chunk_bytes=8)


def test_pack_decoder_validates_fields_digest_and_count() -> None:
    pack = ContextPack.build((b"alpha",))
    payload = json.loads(pack.to_bytes())
    for field in payload:
        incomplete = {key: value for key, value in payload.items() if key != field}
        with pytest.raises(ContextPackError):
            ContextPack.from_bytes(json.dumps(incomplete).encode())
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(pack.to_bytes().replace(pack.digest.encode(), b"sha256:" + b"0" * 64))
    with pytest.raises(ContextPackError):
        ContextPack.from_bytes(json.dumps({**payload, "extra": True}).encode())


def test_delta_supports_any_valid_prefix_and_preserves_target() -> None:
    base = ContextPack.build((b"a", b"b", b"old"))
    target = ContextPack.build((b"a", b"b", b"new", b"tail"))
    delta = ContextPackDelta.between(base, target)
    assert delta.prefix_items == 2
    assert delta.appended == (b"new", b"tail")
    assert apply_context_pack_delta(base, delta).to_bytes() == target.to_bytes()

    manual = {
        **json.loads(delta.to_bytes()),
        "prefix_items": 0,
        "appended": [base64.b64encode(item).decode() for item in target.items],
    }
    decoded = ContextPackDelta.from_bytes(json.dumps(manual).encode())
    assert apply_context_pack_delta(ContextPack.from_bytes(base.to_bytes()), decoded).to_bytes() == target.to_bytes()


def test_delta_and_apply_reject_invalid_inputs() -> None:
    base = ContextPack.build((b"a",))
    delta = ContextPackDelta.between(base, ContextPack.build((b"a", b"b")))

    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build((b"other",)), delta)
    for prefix in (True, 1.5, -1, 2):
        payload = {**json.loads(delta.to_bytes()), "prefix_items": prefix}
        if prefix == 2:
            with pytest.raises(ContextPackError):
                apply_context_pack_delta(base, ContextPackDelta.from_bytes(json.dumps(payload).encode()))
        else:
            with pytest.raises(ContextPackError):
                ContextPackDelta.from_bytes(json.dumps(payload).encode())

    wrong_target = ContextPackDelta(base.digest, "sha256:" + "0" * 64, 0, (b"x",))
    with pytest.raises(ContextPackError):
        apply_context_pack_delta(ContextPack.build(()), wrong_target)


def test_empty_pack_round_trip_and_empty_delta() -> None:
    empty = ContextPack.build(())
    assert ContextPack.from_bytes(empty.to_bytes()) == empty
    assert b"".join(empty.items) == b""
    delta = ContextPackDelta.between(empty, empty)
    assert delta.prefix_items == 0 and delta.appended == ()
    assert apply_context_pack_delta(empty, delta) == empty
    assert delta.byte_count == len(delta.to_bytes())


class _TurnPipeline:
    async def extract(self, request: MemoryCandidateRequest, /) -> tuple[MemoryEntryInput, ...]:
        return tuple(
            MemoryEntryInput(kind="turn", text=source.content, sources=(source,))
            for source in request.sources
            if isinstance(source, ContentSource)
        )


def test_prepare_incremental_rebuilds_prepared_context_and_empty_result() -> None:
    async def scenario() -> None:
        async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=_TurnPipeline()) as runtime:
            scope = await runtime.scopes.create(
                ScopeDraft(title="Pack", summary="Pack", idempotency_key="context-pack-product")
            )
            sources = runtime.sources.for_scope(scope.scope_id)
            memory = runtime.memory.for_scope(scope.scope_id)
            context = runtime.context.for_scope(scope.scope_id)

            await sources.capture(CaptureSource(source_id="turn-1", content="remote control notes", metadata={}))
            while (await memory.flush()).processed:
                pass
            first = await context.prepare_incremental(PrepareContextRequest(query="remote", max_bytes=4096))
            assert isinstance(first, ContextPack)
            assert (
                b"".join(first.items).decode("utf-8")
                == (await context.prepare(PrepareContextRequest(query="remote", max_bytes=4096))).content
            )

            await sources.capture(CaptureSource(source_id="turn-2", content="remote control follow-up", metadata={}))
            while (await memory.flush()).processed:
                pass
            delta = await context.prepare_incremental(
                PrepareContextRequest(query="remote", max_bytes=4096), base_pack=first
            )
            assert isinstance(delta, ContextPackDelta)
            expected = await context.prepare(PrepareContextRequest(query="remote", max_bytes=4096))
            rebuilt = apply_context_pack_delta(
                ContextPack.from_bytes(first.to_bytes()), ContextPackDelta.from_bytes(delta.to_bytes())
            )
            assert b"".join(rebuilt.items).decode("utf-8") == expected.content
            assert delta.byte_count == len(delta.to_bytes())

            empty = await context.prepare_incremental(PrepareContextRequest(query="no-such-term", max_bytes=4096))
            assert isinstance(empty, ContextPack) and empty.items == ()

    asyncio.run(scenario())
