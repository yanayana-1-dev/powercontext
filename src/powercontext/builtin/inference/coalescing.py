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

"""Cross-call embedding request coalescing with a bounded LRU vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage

_DEFAULT_MAX_BATCH_SIZE = 64
_DEFAULT_COALESCE_MS = 5.0
_DEFAULT_CACHE_SIZE = 1024


class _Batch:
    """One in-flight deduplicated provider request shared across waiters."""

    __slots__ = ("texts", "task", "results", "usage", "error", "done")

    def __init__(self, texts: tuple[str, ...]) -> None:
        self.texts = texts
        self.task: asyncio.Task | None = None
        self.results: dict[str, tuple[float, ...]] | None = None
        self.usage: InferenceUsage | None = None
        self.error: BaseException | None = None
        self.done = False


def _aggregate_usage(usages: list[InferenceUsage], /) -> InferenceUsage:
    if not usages:
        return InferenceUsage(requests=0)
    inputs = [usage.input_tokens for usage in usages]
    outputs = [usage.output_tokens for usage in usages]
    return InferenceUsage(
        requests=sum(usage.requests for usage in usages),
        input_tokens=None if all(value is None for value in inputs) else sum(value or 0 for value in inputs),
        output_tokens=None if all(value is None for value in outputs) else sum(value or 0 for value in outputs),
    )


class CoalescingEmbeddingModel:
    """Wrap one embedding model with dedup, cross-call coalescing, and an LRU cache."""

    def __init__(
        self,
        model,
        /,
        *,
        max_batch_size: int = _DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = _DEFAULT_COALESCE_MS,
        cache_size: int = _DEFAULT_CACHE_SIZE,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be non-negative")  # noqa: TRY003
        self._model = model
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._in_flight: dict[str, _Batch] = {}
        self._pending: list[_Batch] = []
        self._flush_handle: asyncio.TimerHandle | None = None

    @property
    def profile(self):
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Embed texts in input order, sharing provider work across concurrent callers."""

        distinct: list[str] = []
        seen: set[str] = set()
        for text in texts:
            if text not in seen:
                seen.add(text)
                distinct.append(text)
        if not distinct:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        resolved: dict[str, tuple[float, ...]] = {}
        waited: list[_Batch] = []
        for text in distinct:
            batch = self._in_flight.get(text)
            if batch is not None:
                if batch not in waited:
                    waited.append(batch)
                continue
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                resolved[text] = cached
                continue
            batch = self._place(text)
            if batch not in waited:
                waited.append(batch)

        self._dispatch_ready()
        if self._coalesce_seconds == 0:
            self._flush()
        else:
            self._schedule_flush()

        for batch in waited:
            await self._await_batch(batch)

        vectors = []
        for text in texts:
            vector = resolved.get(text)
            if vector is None:
                vector = self._in_flight_results(text, waited)
            vectors.append(vector)
        return EmbeddingResult(vectors=tuple(vectors), usage=_aggregate_usage([batch.usage for batch in waited]))

    def _in_flight_results(self, text: str, waited: list[_Batch], /) -> tuple[float, ...]:
        for batch in waited:
            if batch.results is not None and text in batch.results:
                return batch.results[text]
        raise ValueError(f"text {text!r} was not resolved by any shared batch")  # pragma: no cover

    def _place(self, text: str, /) -> _Batch:
        for batch in self._pending:
            if len(batch.texts) < self._max_batch_size:
                batch.texts = (*batch.texts, text)
                self._in_flight[text] = batch
                return batch
        batch = _Batch((text,))
        self._pending.append(batch)
        self._in_flight[text] = batch
        return batch

    def _dispatch_ready(self, /) -> None:
        full = [batch for batch in self._pending if len(batch.texts) >= self._max_batch_size]
        for batch in full:
            self._pending.remove(batch)
            self._start(batch)

    def _flush(self, /) -> None:
        self._cancel_flush_timer()
        for batch in self._pending:
            self._start(batch)
        self._pending.clear()

    def _schedule_flush(self, /) -> None:
        if self._flush_handle is not None or not self._pending:
            return
        loop = asyncio.get_running_loop()
        self._flush_handle = loop.call_later(self._coalesce_seconds, self._flush)

    def _cancel_flush_timer(self, /) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None

    def _start(self, batch: _Batch, /) -> None:
        batch.task = asyncio.create_task(self._run_batch(batch))

    async def _run_batch(self, batch: _Batch, /) -> None:
        try:
            result = await self._model.embed(batch.texts)
            if result.vectors is None or len(result.vectors) != len(batch.texts):
                raise ValueError("embedding model returned a mismatched vector count")  # noqa: TRY003
            batch.results = dict(zip(batch.texts, result.vectors, strict=True))
            batch.usage = result.usage
            if self._cache_size > 0:
                for text, vector in batch.results.items():
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        except BaseException as error:  # noqa: BLE001 - shared with every waiter
            batch.error = error
        finally:
            for text in batch.texts:
                if self._in_flight.get(text) is batch:
                    del self._in_flight[text]
            batch.done = True

    async def _await_batch(self, batch: _Batch, /) -> None:
        if batch.task is None:
            if batch in self._pending:
                self._pending.remove(batch)
            self._start(batch)
        try:
            await asyncio.shield(batch.task)
        except asyncio.CancelledError:
            raise
        if batch.error is not None:
            raise batch.error


__all__ = ["CoalescingEmbeddingModel"]
