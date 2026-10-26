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

"""HTTP cross-encoder reranking for coarse Memory search candidates.

The adapter sends the caller's complete coarse candidate pool to an offline
scoring service in bounded batches. Every candidate keeps its rank in the
original pool, so the service scores from all batches participate in one
global, stable selection before the original Memory hits are projected back.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_POLICY_ID = "http-cross-encoder-v1"


class HTTPMemoryReranker:
    """Score complete Memory candidate pools through one HTTP endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("reranker endpoint must be a non-empty string")  # noqa: TRY003
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
            raise ValueError("reranker batch_size must be a positive integer")  # noqa: TRY003
        self.endpoint = endpoint
        self.policy_id = policy_id
        self._client = client
        self._batch_size = batch_size

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate once and select the globally best ranks."""

        _validate_bounds(len(candidates), limit)
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            ranks = tuple(range(start + 1, start + len(batch) + 1))
            payload = {
                "query": query,
                "candidates": [
                    {"rank": rank, "text": candidate.text} for rank, candidate in zip(ranks, batch, strict=True)
                ],
            }
            requests += 1
            response = await self._client.post(self.endpoint, json=payload)
            response.raise_for_status()
            scores.update(_parse_response_scores(response, ranks))
        if len(scores) != len(candidates):
            raise ValueError("reranker response did not score every candidate exactly once")  # noqa: TRY003
        selected = sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit]
        return MemoryRerankDecision(
            selected_ranks=tuple(selected),
            usage=InferenceUsage(requests=requests),
        )


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


def _parse_response_scores(response: httpx.Response, expected_ranks: tuple[int, ...]) -> dict[int, float]:
    try:
        body = response.json()
    except ValueError as error:
        raise ValueError("reranker response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(body, dict) or set(body) != {"scores"}:
        raise ValueError("reranker response must contain exactly one scores field")  # noqa: TRY003
    entries = body["scores"]
    if not isinstance(entries, list):
        raise ValueError("reranker scores field must be a list")  # noqa: TRY003, TRY004
    expected = set(expected_ranks)
    scores: dict[int, float] = {}
    for entry in entries:
        rank, score = _parse_score_entry(entry)
        if rank not in expected:
            raise ValueError(f"reranker response contains unexpected rank {rank!r}")  # noqa: TRY003
        if rank in scores:
            raise ValueError(f"reranker response repeats rank {rank!r}")  # noqa: TRY003
        scores[rank] = score
    if set(scores) != expected:
        raise ValueError("reranker response must cover every submitted rank exactly once")  # noqa: TRY003
    return scores


def _parse_score_entry(entry: Any) -> tuple[int, float]:
    if not isinstance(entry, dict) or set(entry) != {"rank", "score"}:
        raise ValueError("each reranker score must contain exactly rank and score")  # noqa: TRY003
    rank = entry["rank"]
    if not isinstance(rank, int) or isinstance(rank, bool):
        raise ValueError("reranker score rank must be an integer")  # noqa: TRY003, TRY004
    score = entry["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("reranker score must be a number")  # noqa: TRY003, TRY004
    value = float(score)
    if not math.isfinite(value):
        raise ValueError("reranker score must be finite")  # noqa: TRY003
    return rank, value


__all__ = ["DEFAULT_POLICY_ID", "HTTPMemoryReranker"]
