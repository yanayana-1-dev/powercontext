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

import json

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

REF = ArtifactRef(family="memory", artifact_id="memory", revision=2)


def _hit(entry_id: str, text: str) -> MemoryHit:
    return MemoryHit(
        memory_ref=REF,
        entry_id=entry_id,
        entry_version_id="version-1",
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def _candidates(scope: str, *hits: MemoryHit) -> PreparedMemoryCandidates:
    return PreparedMemoryCandidates(scope_id=scope, memory_ref=REF, hits=hits)


def test_query_selects_late_literal_evidence_and_keeps_exact_offsets() -> None:
    prefix = "General administrative notes. " * 70
    alpha = _hit("alpha", prefix + "The cobalt sensor is calibrated at 37 degrees.")
    beta = _hit("beta", prefix + "The amber valve opens at 19 degrees.")
    candidates = _candidates("scope-a", alpha, beta)
    first = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt sensor calibrated", max_bytes=1200, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(candidates,),
    )
    second = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="amber valve opens", max_bytes=1200, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(candidates,),
    )
    assert any("37" in fragment.text for fragment in first.fragments)
    assert any("19" in fragment.text for fragment in second.fragments)
    assert first.context.content_bytes == len(first.context.content.encode("utf-8"))
    assert first.context.content_bytes <= 1200
    for fragment in (*first.fragments, *second.fragments):
        source = alpha.text if fragment.address.entry_id == "alpha" else beta.text
        assert 0 <= fragment.start < fragment.end <= len(source)
        assert fragment.text == source[fragment.start : fragment.end]


def test_fragments_origins_and_envelope_items_align() -> None:
    text = "Intro. The cobalt sensor is calibrated at 37 degrees. Tail."
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=1400, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(_candidates("scope-a", _hit("entry", text)),),
    )
    assert build.fragments
    assert len(build.origins) == len(build.fragments)
    items = json.loads(build.context.content.splitlines()[-2])["items"]
    assert len(items) == len(build.fragments)
    for item, fragment, origin in zip(items, build.fragments, build.origins, strict=True):
        assert isinstance(origin, MemoryEntryAddress)
        assert origin == fragment.address
        assert item["content"] == fragment.text
        assert item["start"] == fragment.start and item["end"] == fragment.end
        assert item["citation"] == {
            "scope_id": "scope-a",
            "memory_ref": REF.model_dump(mode="json"),
            "entry_id": fragment.address.entry_id,
            "entry_version_id": fragment.address.entry_version_id,
        }
    assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in build.context.content
    assert "END_POWERCONTEXT_PREPARED_CONTEXT_V1" in build.context.content
    assert "untrusted_history" in build.context.content


def test_unicode_budget_and_empty_candidate_result() -> None:
    text = "海洋观测 station records temperatures. " * 40
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="海洋观测 station", max_bytes=1200, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(_candidates("scope-a", _hit("entry", text)),),
    )
    assert build.fragments
    assert build.context.content_bytes == len(build.context.content.encode("utf-8")) <= 1200
    empty = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="nothing", max_bytes=1200, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(),
    )
    assert empty.fragments == () and empty.origins == ()
    assert empty.context.status == "empty"
    assert empty.context.content is None and empty.context.content_bytes == 0


def test_cross_scope_address_and_repeated_entry_are_deduplicated() -> None:
    hit = _hit("entry", "The cobalt sensor is calibrated at 37 degrees.")
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=1400, evidence_selection=True),
        current_scope_id="scope-a",
        memory_candidates=(_candidates("scope-b", hit), _candidates("scope-b", hit)),
    )
    assert build.fragments
    address = build.fragments[0].address
    assert address.memory == ArtifactAddress(scope_id="scope-b", artifact=REF)
    assert len({fragment.address.entry_id for fragment in build.fragments}) == 1


def test_default_prepare_keeps_ordinary_behavior() -> None:
    build = PreparedContextBuilder().build_scopes_result(
        request=PrepareContextRequest(query="cobalt", max_bytes=8000),
        current_scope_id="scope-a",
        memory_candidates=(_candidates("scope-a", _hit("entry", "A station records cobalt temperature.")),),
    )
    assert build.fragments == ()
    assert build.context.content is not None
    assert "A station records cobalt temperature." in build.context.content
    assert len(build.origins) == 1
