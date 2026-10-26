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
import json

import httpx
import pytest

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.artifacts.memory.http_reranker import HTTPMemoryReranker
from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import BuiltinConfig, open_builtin_contexts


def _hit(rank: int) -> MemoryHit:
    return MemoryHit(
        memory_ref=ArtifactRef(family="memory", artifact_id="m", revision=1),
        entry_id=f"e{rank}",
        entry_version_id=f"v{rank}",
        text=f"candidate {rank}",
        score=1.0 / rank,
        matched_by=("fts",),
    )


def _run(coroutine) -> None:
    asyncio.run(coroutine)


def test_batches_cover_every_rank_once_and_select_globally() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"] == "sensor calibration"
        assert 1 <= len(body["candidates"]) <= 2
        for item in body["candidates"]:
            assert set(item) == {"rank", "text"}
            assert item["text"] == _hit(item["rank"]).text
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "scores": [
                    {"rank": item["rank"], "score": {1: 0.9, 2: 0.9, 3: 0.95, 4: 0.9}[item["rank"]]}
                    for item in reversed(body["candidates"])
                ]
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=2)
            decision = await reranker.rerank(
                "sensor calibration",
                tuple(_hit(rank) for rank in range(1, 5)),
                2,
            )
        assert decision.selected_ranks == (3, 1)
        assert decision.usage.requests == 2
        assert len(requests) == 2
        assert sorted(item["rank"] for request in requests for item in request["candidates"]) == [1, 2, 3, 4]

    _run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"scores": [], "extra": True},
        {"scores": {}},
        {"score": []},
        {"scores": [{"rank": 1, "score": 0.5, "extra": True}]},
        {"scores": [{"rank": 1}]},
        {"scores": [{"rank": 2, "score": 0.5}]},
        {"scores": [{"rank": 1, "score": 0.5}, {"rank": 1, "score": 0.4}]},
        {"scores": [{"rank": 1, "score": float("nan")}]},
        {"scores": [{"rank": 1, "score": float("inf")}]},
        {"scores": [{"rank": 1, "score": "0.5"}]},
        {"scores": [{"rank": True, "score": 0.5}]},
        {"scores": [{"rank": 1.0, "score": 0.5}]},
    ],
)
def test_invalid_response_schemas_raise_value_error(payload) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://invalid", client=client).rerank("q", (_hit(1),), 1)

    _run(scenario())


def test_invalid_json_and_cross_batch_ranks_fail() -> None:
    def bad_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    def cross_batch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ranks = [item["rank"] for item in body["candidates"]]
        return httpx.Response(200, json={"scores": [{"rank": rank + 2, "score": 0.5} for rank in ranks]})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(bad_json)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://invalid", client=client).rerank("q", (_hit(1),), 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(cross_batch)) as client:
            with pytest.raises(ValueError):
                await HTTPMemoryReranker("https://invalid", client=client, batch_size=1).rerank(
                    "q", (_hit(1), _hit(2)), 1
                )

    _run(scenario())


def test_http_and_transport_errors_are_preserved() -> None:
    def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"scores": []})

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(server_error)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await HTTPMemoryReranker("https://invalid", client=client).rerank("q", (_hit(1),), 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as client:
            with pytest.raises(httpx.ConnectError):
                await HTTPMemoryReranker("https://invalid", client=client).rerank("q", (_hit(1),), 1)

    _run(scenario())


def test_constructor_and_limit_bounds_raise_value_error() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(200))) as client:
            with pytest.raises(ValueError):
                HTTPMemoryReranker("", client=client)
            with pytest.raises(ValueError):
                HTTPMemoryReranker("https://invalid", client=client, batch_size=0)
            reranker = HTTPMemoryReranker("https://invalid", client=client, batch_size=4)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (), 1)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 0)
            with pytest.raises(ValueError):
                await reranker.rerank("q", (_hit(1),), 2)

    _run(scenario())


def test_native_memory_search_preserves_the_selected_entry_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={"scores": [{"rank": item["rank"], "score": float(item["rank"])} for item in body["candidates"]]},
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            reranker = HTTPMemoryReranker("https://reranker.invalid/score", client=client, batch_size=1)
            async with open_builtin_contexts(
                BuiltinConfig(database=SQLiteConfig()), memory_reranker=reranker
            ) as contexts:
                service = (await contexts.get("retrieval-01-reranking-product")).artifacts.memory
                memory = await service.remember(
                    memory=None,
                    entries=(
                        MemoryEntryInput(kind="fact", text="Project alpha fact."),
                        MemoryEntryInput(kind="fact", text="Project beta fact."),
                    ),
                    mode="append",
                )
                assert memory is not None
                result = await service.search("project", memories=(memory,), limit=1, mode="fts")
                assert result.rerank is not None
                assert result.rerank.selected_ranks == (len(result.rerank.candidate_hits),)
                assert result.hits[0].entry_version_id == result.rerank.candidate_hits[-1].entry_version_id
                assert result.rerank.usage.requests == len(result.rerank.candidate_hits)

    _run(scenario())
