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

"""Cross-call Embedding request coalescing and a bounded success cache.

Concurrent callers that share one wrapper join per-text futures. The wrapper
deduplicates text, batches unique misses within one coalescing window, keeps an
LRU cache of successful vectors, and reports each caller's share of the
provider usage without letting one caller's cancellation cancel shared work.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict, deque
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage


@dataclass(slots=True)
class _Entry:
    """One text shared by every caller that currently waits for it."""

    future: asyncio.Future[tuple[float, ...]]
    batch_id: int | None = None
    usage: InferenceUsage | None = None


class CoalescingEmbeddingModel:
    """Merge concurrent Embedding calls and cache successful vectors."""

    def __init__(
        self,
        model: Any,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if not isinstance(max_batch_size, int) or isinstance(max_batch_size, bool) or max_batch_size < 1:
            raise ValueError("max_batch_size must be a positive integer")  # noqa: TRY003
        if (
            not isinstance(coalesce_ms, (int, float))
            or isinstance(coalesce_ms, bool)
            or not math.isfinite(float(coalesce_ms))
            or coalesce_ms < 0
        ):
            raise ValueError("coalesce_ms must be a finite non-negative number")  # noqa: TRY003
        if not isinstance(cache_size, int) or isinstance(cache_size, bool) or cache_size < 0:
            raise ValueError("cache_size must be a non-negative integer")  # noqa: TRY003
        self._model = model
        self.profile = model.profile
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = float(coalesce_ms) / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._entries: dict[str, _Entry] = {}
        self._queue: deque[str] = deque()
        self._flush_scheduled = False
        self._next_batch_id = 0
        self._tasks: set[asyncio.Task[None]] = set()

    async def embed(self, texts: Iterable[str]) -> EmbeddingResult:
        """Return one vector per input position, sharing work across callers."""

        values = tuple(texts)
        if not values:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        unique = tuple(dict.fromkeys(values))
        vectors: dict[str, tuple[float, ...]] = {}
        waiting: dict[str, _Entry] = {}
        scheduled = False
        loop = asyncio.get_running_loop()
        for text in unique:
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                vectors[text] = cached
                continue
            entry = self._entries.get(text)
            if entry is None:
                entry = _Entry(future=loop.create_future())
                self._entries[text] = entry
                self._queue.append(text)
                scheduled = True
            waiting[text] = entry
        if scheduled:
            self._schedule_flush()
        batch_usage: dict[int, InferenceUsage] = {}
        for text, entry in waiting.items():
            vectors[text] = await asyncio.shield(entry.future)
            if entry.batch_id is not None and entry.usage is not None:
                batch_usage.setdefault(entry.batch_id, entry.usage)
        return EmbeddingResult(vectors=tuple(vectors[text] for text in values), usage=_sum_usage(batch_usage.values()))

    def _schedule_flush(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        self._spawn(self._flush())

    async def _flush(self) -> None:
        if self._coalesce_seconds > 0:
            await asyncio.sleep(self._coalesce_seconds)
        else:
            await asyncio.sleep(0)
        self._flush_scheduled = False
        batch: list[str] = []
        while self._queue and len(batch) < self._max_batch_size:
            batch.append(self._queue.popleft())
        if self._queue:
            self._schedule_flush()
        if not batch:
            return
        batch_id = self._next_batch_id
        self._next_batch_id += 1
        for text in batch:
            entry = self._entries.get(text)
            if entry is not None:
                entry.batch_id = batch_id
        self._spawn(self._run_batch(batch_id, tuple(batch)))

    async def _run_batch(self, batch_id: int, texts: tuple[str, ...]) -> None:
        try:
            result = await self._model.embed(texts)
            vectors = tuple(tuple(float(value) for value in vector) for vector in result.vectors)
            _require_vector_count(vectors, len(texts))
            for text, vector in zip(texts, vectors, strict=True):
                self._resolve(text, vector, batch_id, result.usage)
        except Exception as error:
            for text in texts:
                self._reject(text, error)

    def _resolve(self, text: str, vector: tuple[float, ...], batch_id: int, usage: InferenceUsage) -> None:
        entry = self._entries.pop(text, None)
        if entry is None:
            return
        entry.usage = usage
        if self._cache_size:
            self._cache[text] = vector
            self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        if not entry.future.done():
            entry.future.set_result(vector)

    def _reject(self, text: str, error: Exception) -> None:
        entry = self._entries.pop(text, None)
        if entry is None:
            return
        if not entry.future.done():
            entry.future.set_exception(error)
            # Claim the exception so a fully cancelled waiter set does not emit a warning.
            entry.future.exception()

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _require_vector_count(vectors: tuple[tuple[float, ...], ...], expected: int) -> None:
    if len(vectors) != expected:
        raise ValueError("embedding vector count does not match the batch size")  # noqa: TRY003


def _sum_usage(usages: Iterable[InferenceUsage]) -> InferenceUsage:
    items = tuple(usages)
    if not items:
        return InferenceUsage(requests=0)
    return InferenceUsage(
        requests=sum(item.requests for item in items),
        input_tokens=_sum_optional(item.input_tokens for item in items),
        output_tokens=_sum_optional(item.output_tokens for item in items),
    )


def _sum_optional(values: Iterable[int | None]) -> int | None:
    numbers = [value for value in values if value is not None]
    return sum(numbers) if numbers else None


__all__ = ["CoalescingEmbeddingModel"]
