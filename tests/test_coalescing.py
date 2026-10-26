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

from powercontext.builtin.artifacts.memory import EmbeddingProfile
from powercontext.builtin.inference import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.coalescing import CoalescingEmbeddingModel


class _Embedding:
    profile = EmbeddingProfile(profile_id="coalescing-product", model="local", dimension=1)

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts):
        self.calls.append(tuple(texts))
        return EmbeddingResult(
            vectors=tuple((float(len(text)),) for text in texts),
            usage=InferenceUsage(requests=1),
        )


def test_defaults_profile_and_empty_input() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base)
        assert model.profile == base.profile
        result = await model.embed(())
        assert result.vectors == () and result.usage.requests == 0
        assert base.calls == []

    asyncio.run(scenario())


def test_concurrent_calls_share_work_and_restore_positions() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, max_batch_size=2, coalesce_ms=10, cache_size=4)
        first, second = await asyncio.gather(
            model.embed(("alpha", "beta", "alpha")),
            model.embed(("beta", "gamma")),
        )
        assert first.vectors == ((5.0,), (4.0,), (5.0,))
        assert second.vectors == ((4.0,), (5.0,))
        assert all(len(call) <= 2 for call in base.calls)
        assert sorted(text for call in base.calls for text in call) == ["alpha", "beta", "gamma"]
        assert first.usage.requests == sum(bool(set(call) & {"alpha", "beta"}) for call in base.calls)
        assert second.usage.requests == sum(bool(set(call) & {"beta", "gamma"}) for call in base.calls)

    asyncio.run(scenario())


def test_late_caller_joins_in_flight_work_even_without_cache() -> None:
    class Blocked(_Embedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts):
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=((1.0,),))

    async def scenario() -> None:
        base = Blocked()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        first = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        second = asyncio.create_task(model.embed(("shared",)))
        await asyncio.sleep(0)
        base.release.set()
        assert (await first).vectors == ((1.0,),)
        assert (await second).vectors == ((1.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())


def test_lru_eviction_access_refresh_and_disabled_cache() -> None:
    async def scenario() -> None:
        base = _Embedding()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=2)
        await model.embed(("alpha",))
        await model.embed(("beta",))
        await model.embed(("alpha",))
        await model.embed(("gamma",))
        await model.embed(("alpha",))
        assert sum(call.count("alpha") for call in base.calls) == 1
        await model.embed(("beta",))
        assert sum(call.count("beta") for call in base.calls) == 2

        disabled = CoalescingEmbeddingModel(base, coalesce_ms=0, cache_size=0)
        await disabled.embed(("delta",))
        await disabled.embed(("delta",))
        assert sum(call.count("delta") for call in base.calls) == 2

    asyncio.run(scenario())


def test_failure_is_not_cached_and_independent_batches_still_run() -> None:
    class FailOnce(_Embedding):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def embed(self, texts):
            self.calls.append(tuple(texts))
            if texts == ("bad",) and not self.failed:
                self.failed = True
                raise RuntimeError("temporary")
            return EmbeddingResult(vectors=tuple((1.0,) for _ in texts))

    async def scenario() -> None:
        base = FailOnce()
        model = CoalescingEmbeddingModel(base, max_batch_size=1, coalesce_ms=0)
        with pytest.raises(RuntimeError):
            await model.embed(("bad", "good"))
        await asyncio.sleep(0.05)
        assert ("good",) in base.calls
        assert (await model.embed(("bad",))).vectors == ((1.0,),)
        assert sum(call.count("bad") for call in base.calls) == 2

    asyncio.run(scenario())


def test_vector_count_mismatch_and_usage_summing() -> None:
    class Invalid(_Embedding):
        async def embed(self, texts):
            return EmbeddingResult(vectors=())

    class Metered(_Embedding):
        async def embed(self, texts):
            self.calls.append(tuple(texts))
            return EmbeddingResult(
                vectors=tuple((1.0,) for _ in texts),
                usage=InferenceUsage(requests=1, input_tokens=5, output_tokens=2),
            )

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await CoalescingEmbeddingModel(Invalid(), coalesce_ms=0).embed(("broken",))
        model = CoalescingEmbeddingModel(Metered(), max_batch_size=1, coalesce_ms=0)
        result = await model.embed(("one", "two"))
        assert result.usage.requests == 2
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 4
        cached = await model.embed(("one",))
        assert cached.usage.requests == 0
        assert cached.usage.input_tokens is None

    asyncio.run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_work() -> None:
    class Blocked(_Embedding):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts):
            self.calls.append(tuple(texts))
            self.started.set()
            await self.release.wait()
            return EmbeddingResult(vectors=((2.0,),))

    async def scenario() -> None:
        base = Blocked()
        model = CoalescingEmbeddingModel(base, coalesce_ms=0)
        cancelled = asyncio.create_task(model.embed(("shared",)))
        survivor = asyncio.create_task(model.embed(("shared",)))
        await base.started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        base.release.set()
        assert (await survivor).vectors == ((2.0,),)
        assert base.calls == [("shared",)]

    asyncio.run(scenario())
