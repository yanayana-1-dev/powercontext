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

"""HTTP scoring-service reranker with bounded batches and cross-batch ordering."""

from __future__ import annotations

import json
import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

_DEFAULT_BATCH_SIZE = 32
_DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Rerank coarse Memory candidates with one batched HTTP scoring service."""

    def __init__(
        self,
        endpoint: str,
        /,
        *,
        client: httpx.AsyncClient,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        policy_id: str = _DEFAULT_POLICY_ID,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self.endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once in bounded batches and select the global top ranks."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + len(batch) + 1))
            payload = {
                "query": query,
                "candidates": [
                    {"rank": rank, "text": candidate.text}
                    for rank, candidate in zip(ranks, batch, strict=True)
                ],
            }
            response = await self._client.post(self.endpoint, json=payload)
            response.raise_for_status()
            batch_scores = _parse_scores(response)
            _require_batch_coverage(batch_scores, ranks)
            scores.update(batch_scores)
            requests += 1
        selected = sorted(range(1, len(candidates) + 1), key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(selected),
            usage=InferenceUsage(requests=requests),
        )


def _validate_bounds(candidate_count: int, limit: int, /) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


def _parse_scores(response: httpx.Response, /) -> list[tuple[int, float]]:
    try:
        payload = json.loads(response.content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("reranker response must be JSON") from error
    if not isinstance(payload, dict) or set(payload) != {"scores"}:
        raise ValueError("reranker response must contain exactly the scores field")
    entries = payload["scores"]
    if not isinstance(entries, list):
        raise ValueError("scores must be an array")
    scores: list[tuple[int, float]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"rank", "score"}:
            raise ValueError("each score record must contain exactly rank and score")
        rank = entry["rank"]
        score = entry["score"]
        if type(rank) is not int:
            raise ValueError("score rank must be an integer")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("score must be a finite number")
        scores.append((rank, float(score)))
    return scores


def _require_batch_coverage(scores: list[tuple[int, float]], ranks: tuple[int, ...], /) -> None:
    expected = sorted(ranks)
    observed = sorted(rank for rank, _score in scores)
    if observed != expected:
        raise ValueError("reranker response must cover exactly this batch's ranks once each")


__all__ = ["HTTPMemoryReranker"]
