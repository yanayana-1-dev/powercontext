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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain, pairwise
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_MIN_FRAGMENT_CHARS = 24
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_SENTENCE_BOUNDARY = re.compile(r"[.!?]+(?=\s|$)|\n+")
_STOPWORDS = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "for",
    "on",
    "with",
    "by",
    "from",
    "as",
    "at",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "that",
    "this",
    "these",
    "those",
    "it",
    "its",
    "their",
    "our",
    "your",
    "his",
    "her",
    "they",
    "we",
    "you",
    "i",
    "not",
    "no",
    "do",
    "does",
    "did",
    "has",
    "have",
    "had",
    "can",
    "could",
    "may",
    "might",
    "will",
    "would",
    "should",
    "about",
    "into",
    "over",
    "under",
    "between",
    "during",
    "before",
    "after",
    "than",
    "then",
    "so",
    "such",
    "more",
    "most",
    "very",
    "much",
    "many",
    "any",
    "all",
    "each",
    "both",
    "either",
    "neither",
    "other",
    "another",
    "same",
    "own",
    "only",
    "just",
    "also",
])
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class _FragmentCandidate:
    """One possible literal slice before the byte budget is applied."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str
    score: float
    trim: bool = True


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[PreparedFragment, ...] = ()


@dataclass(frozen=True)
class PreparedFragment:
    """One literal slice of a Memory entry selected for the current query."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope."""

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 16
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    entry_limit = 8
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        if request.evidence_selection:
            if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
                raise PreparedContextInvariantError("memory-candidate-limit")
            return self._build_fragment_result(request=request, memory_candidates=memory_candidates)
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
        )

    def _build_fragment_result(
        self,
        *,
        request: PrepareContextRequest,
        memory_candidates: Sequence[PreparedMemoryCandidates],
    ) -> PreparedContextBuild:
        fragments = self._select_fragments(request=request, memory_candidates=memory_candidates)
        if not fragments:
            return PreparedContextBuild(context=self.empty(), origins=(), fragments=())
        content = _render_fragments(fragments)
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > request.max_bytes:
            return PreparedContextBuild(context=self.empty(), origins=(), fragments=())
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=tuple(fragment.address for fragment in fragments),
            fragments=fragments,
        )

    def _select_fragments(  # noqa: C901 - one scored candidate pass and one budget pass
        self,
        *,
        request: PrepareContextRequest,
        memory_candidates: Sequence[PreparedMemoryCandidates],
    ) -> tuple[PreparedFragment, ...]:
        entries: list[tuple[MemoryEntryAddress, str]] = []
        seen_entries: set[tuple[str, str, int, str, str]] = set()
        for candidates in memory_candidates:
            if candidates.hits and candidates.memory_ref is None:
                raise PreparedContextInvariantError("memory-ref-missing")
            for hit in candidates.hits:
                if hit.memory_ref != candidates.memory_ref:
                    raise PreparedContextInvariantError("memory-ref-mismatch")
                if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                    continue
                memory = ArtifactAddress(scope_id=candidates.scope_id, artifact=hit.memory_ref)
                identity = (
                    memory.scope_id,
                    hit.memory_ref.artifact_id,
                    hit.memory_ref.revision,
                    hit.entry_id,
                    hit.entry_version_id,
                )
                if identity in seen_entries:
                    continue
                seen_entries.add(identity)
                address = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                entries.append((address, hit.text))
        if not entries:
            return ()

        query_tokens = _TOKEN_PATTERN.findall(request.query.lower())
        content_terms = [term for term in dict.fromkeys(query_tokens) if term not in _STOPWORDS]
        if not content_terms:
            content_terms = list(dict.fromkeys(query_tokens))
        sentence_rows: list[tuple[MemoryEntryAddress, str, int, int, tuple[str, ...]]] = []
        for address, text in entries:
            for start, end in _sentence_spans(text):
                tokens = tuple(_TOKEN_PATTERN.findall(text[start:end].lower()))
                if tokens:
                    sentence_rows.append((address, text, start, end, tokens))
        if not sentence_rows:
            return ()

        document_frequency = Counter()
        for _address, _text, _start, _end, tokens in sentence_rows:
            document_frequency.update({_stem(token) for token in tokens})
        content_stems = {term: _stem(term) for term in content_terms}
        idf = {
            _stem(term): math.log((len(sentence_rows) + 1) / (document_frequency.get(_stem(term), 0) + 0.5)) + 1.0
            for term in set(query_tokens)
        }
        query_bigrams = tuple(dict.fromkeys(pairwise(query_tokens)))
        stem_bigrams = tuple(
            dict.fromkeys(
                (content_stems.get(left, _stem(left)), content_stems.get(right, _stem(right)))
                for left, right in query_bigrams
            )
        )
        normalized_query = " ".join(query_tokens)

        candidates: list[_FragmentCandidate] = []
        by_entry: dict[tuple[str, str, int, str, str], list[tuple[int, int, str, float, MemoryEntryAddress]]] = {}
        sentence_counts = Counter(_address_key(address) for address, _text, _start, _end, _tokens in sentence_rows)
        for address, text, start, end, tokens in sentence_rows:
            score = _sentence_score(
                tokens,
                content_terms,
                content_stems,
                idf,
                query_bigrams,
                stem_bigrams,
                normalized_query,
            )
            if start == 0 and sentence_counts[_address_key(address)] > 1:
                score *= 0.2
            if score > 0:
                candidates.append(
                    _FragmentCandidate(address=address, start=start, end=end, text=text[start:end], score=score)
                )
            key = _address_key(address)
            by_entry.setdefault(key, []).append((start, end, text, score, address))
        for rows in by_entry.values():
            rows.sort(key=lambda row: row[0])
            for width in (2, 3):
                for index in range(len(rows) - width + 1):
                    subset = rows[index : index + width]
                    window_score = sum(row[3] for row in subset) * (0.9 if width == 2 else 0.8)
                    if window_score <= 0:
                        continue
                    first, last = subset[0], subset[-1]
                    candidates.append(
                        _FragmentCandidate(
                            address=first[4],
                            start=first[0],
                            end=last[1],
                            text=first[2][first[0] : last[1]],
                            score=window_score,
                            trim=False,
                        )
                    )
            if rows:
                entry_score = sum(row[3] for row in rows) * 0.75
                if entry_score > 0:
                    candidates.append(
                        _FragmentCandidate(
                            address=rows[0][4],
                            start=rows[0][0],
                            end=rows[-1][1],
                            text=rows[0][2][rows[0][0] : rows[-1][1]],
                            score=entry_score,
                            trim=False,
                        )
                    )

        unique: dict[tuple[str, str, int, str, str, int, int], _FragmentCandidate] = {}
        for candidate in candidates:
            key = (*_address_key(candidate.address), candidate.start, candidate.end)
            previous = unique.get(key)
            if previous is None or candidate.score > previous.score:
                unique[key] = candidate
        ranked = sorted(unique.values(), key=lambda candidate: (-candidate.score, candidate.end - candidate.start))
        fragments: list[PreparedFragment] = []
        for candidate in ranked:
            if any(
                fragment.address == candidate.address
                and candidate.start < fragment.end
                and fragment.start < candidate.end
                for fragment in fragments
            ):
                continue
            fragment = self._fit_candidate(candidate, fragments, request.max_bytes)
            if fragment is not None:
                fragments.append(fragment)
        if fragments:
            return self._merge_fragments(
                self._expand_fragments(fragments, by_entry, request.max_bytes),
                by_entry,
                request.max_bytes,
            )

        # No query term matched any candidate: still deliver a literal prefix when it fits.
        address, text = entries[0]
        spans = _sentence_spans(text)
        start, end = spans[0] if spans else (0, len(text))
        fallback = _FragmentCandidate(address=address, start=start, end=end, text=text[start:end], score=0.0)
        fitted = self._fit_candidate(fallback, (), request.max_bytes)
        return () if fitted is None else (fitted,)

    def _expand_fragments(
        self,
        fragments: Sequence[PreparedFragment],
        by_entry: dict[tuple[str, str, int, str, str], list[tuple[int, int, str, float, MemoryEntryAddress]]],
        max_bytes: int,
    ) -> tuple[PreparedFragment, ...]:
        expanded = list(fragments)
        changed = True
        while changed:
            changed = False
            for index, fragment in enumerate(expanded):
                rows = by_entry.get(_address_key(fragment.address))
                if not rows:
                    continue
                rows = sorted(rows, key=lambda row: row[0])
                text = rows[0][2]
                successors = [
                    row for row in rows if fragment.end <= row[0] <= fragment.end + 2 and row[1] > fragment.end
                ]
                if successors:
                    row = successors[0]
                    if not any(
                        other is not fragment
                        and other.address == fragment.address
                        and other.start < row[1]
                        and row[0] < other.end
                        for other in expanded
                    ):
                        attempt = PreparedFragment(
                            address=fragment.address,
                            start=fragment.start,
                            end=row[1],
                            text=text[fragment.start : row[1]],
                        )
                        trial = list(expanded)
                        trial[index] = attempt
                        if _fragments_bytes(trial) <= max_bytes:
                            expanded[index] = attempt
                            fragment = attempt
                            changed = True
                predecessors = [
                    row for row in rows if fragment.start - 2 <= row[1] <= fragment.start and row[0] < fragment.start
                ]
                if predecessors:
                    row = predecessors[-1]
                    if not any(
                        other is not fragment
                        and other.address == fragment.address
                        and other.start < fragment.end
                        and row[0] < other.end
                        for other in expanded
                    ):
                        attempt = PreparedFragment(
                            address=fragment.address,
                            start=row[0],
                            end=fragment.end,
                            text=text[row[0] : fragment.end],
                        )
                        trial = list(expanded)
                        trial[index] = attempt
                        if _fragments_bytes(trial) <= max_bytes:
                            expanded[index] = attempt
                            changed = True
        return tuple(expanded)

    def _merge_fragments(
        self,
        fragments: Sequence[PreparedFragment],
        by_entry: dict[tuple[str, str, int, str, str], list[tuple[int, int, str, float, MemoryEntryAddress]]],
        max_bytes: int,
    ) -> tuple[PreparedFragment, ...]:
        groups: dict[tuple[str, str, int, str, str], list[PreparedFragment]] = {}
        for fragment in fragments:
            groups.setdefault(_address_key(fragment.address), []).append(fragment)
        merged = list(fragments)
        for group in groups.values():
            if len(group) < 2:
                continue
            rows = by_entry.get(_address_key(group[0].address))
            if not rows:
                continue
            text = rows[0][2]
            start = min(fragment.start for fragment in group)
            end = max(fragment.end for fragment in group)
            replacement = PreparedFragment(address=group[0].address, start=start, end=end, text=text[start:end])
            trial = [fragment for fragment in merged if fragment not in group]
            trial.append(replacement)
            if _fragments_bytes(trial) <= max_bytes:
                merged = trial
        return tuple(merged)

    def _fit_candidate(
        self,
        candidate: _FragmentCandidate,
        fragments: Sequence[PreparedFragment],
        max_bytes: int,
    ) -> PreparedFragment | None:
        full = PreparedFragment(
            address=candidate.address,
            start=candidate.start,
            end=candidate.end,
            text=candidate.text,
        )
        if _fragments_bytes((*fragments, full)) <= max_bytes:
            return full
        if not candidate.trim:
            return None
        lower = candidate.start + _MIN_FRAGMENT_CHARS
        upper = candidate.end
        best: PreparedFragment | None = None
        while lower <= upper:
            end = (lower + upper) // 2
            text = candidate.text[: end - candidate.start]
            if len(text.strip()) < _MIN_FRAGMENT_CHARS:
                lower = end + 1
                continue
            attempt = PreparedFragment(address=candidate.address, start=candidate.start, end=end, text=text)
            if _fragments_bytes((*fragments, attempt)) <= max_bytes:
                best = attempt
                lower = end + 1
            else:
                upper = end - 1
        return best

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        if request.evidence_selection:
            fragments = self._select_fragments(request=request, memory_candidates=memory_candidates)
            selected = _render_fragments(fragments) if fragments else None
            content_bytes = 0 if selected is None else len(selected.encode("utf-8"))
            return RecallBudgetView(
                max_bytes=request.max_bytes,
                delivered_items=len(fragments),
                truncated_items=0,
                dropped_items=0,
                unused_bytes=max(0, request.max_bytes - content_bytes),
            )
        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions)``; ``content`` is ``None`` exactly when nothing
        was selected, which is the empty-result case for both paths. This is the single
        selection path shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the
        probe's counters always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            return self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )

        memory_entries = _interleave_groups(
            tuple(
                self._memory_entries(
                    candidates.memory_ref,
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in memory_candidates
            )
        )
        topic_memory_entries = self._topic_memory_entries(topic_memory_hits)
        experience_entries = _interleave_groups(
            tuple(
                self._experience_entries(
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in experience_candidates
            )
        )[: self.experience_entry_limit]
        entries, omissions = self._fit_entries(request, memory_entries, topic_memory_entries, experience_entries)
        if not entries:
            return None, (), omissions
        return _render(entries), tuple(entry.origin for entry in entries), omissions

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            for entry in entries:
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                rank += 1
                outcome = fit_context_text_item(included, replace(item, recall_rank=rank), assembly, request.max_bytes)
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                selected_count += 1
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for candidate in _interleave(memory_entries, topic_memory_entries, experience_entries):
            if len(entries) >= self.entry_limit:
                break
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=request.max_bytes,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate)) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate)) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _sentence_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        spans.append((start, match.end()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    result: list[tuple[int, int]] = []
    for raw_start, raw_end in spans:
        segment = text[raw_start:raw_end]
        leading = len(segment) - len(segment.lstrip())
        trailing = len(segment) - len(segment.rstrip())
        begin = raw_start + leading
        end = raw_end - trailing
        if begin < end and text[begin:end].strip():
            result.append((begin, end))
    return tuple(result)


_STEM_SUFFIXES = (
    "ational",
    "ization",
    "izations",
    "ities",
    "ivity",
    "iveness",
    "ments",
    "ment",
    "ness",
    "ingly",
    "edly",
    "ing",
    "ed",
    "ies",
    "ive",
    "ions",
    "ion",
    "ally",
    "al",
    "ics",
    "ic",
    "s",
)


def _stem(token: str) -> str:
    for suffix in _STEM_SUFFIXES:
        if not token.endswith(suffix):
            continue
        if suffix == "s" and token.endswith(("sis", "us", "is")):
            continue
        base = token[: -len(suffix)]
        if len(base) < 4:
            continue
        return f"{base}y" if suffix == "ies" else base
    return token


def _sentence_score(
    tokens: Sequence[str],
    content_terms: Sequence[str],
    content_stems: dict[str, str],
    idf: dict[str, float],
    query_bigrams: Sequence[tuple[str, str]],
    stem_bigrams: Sequence[tuple[str, str]],
    normalized_query: str,
) -> float:
    stems = Counter(_stem(token) for token in tokens)
    normalized = " ".join(tokens)
    normalized_stems = " ".join(_stem(token) for token in tokens)
    score = 0.0
    for term in content_terms:
        stem = content_stems.get(term, _stem(term))
        frequency = stems.get(stem, 0)
        if frequency:
            score += idf.get(stem, 1.0) * (1.0 + math.log(frequency))
    for left, right in query_bigrams:
        if f"{left} {right}" in normalized:
            score += 0.75 * (idf.get(_stem(left), 1.0) + idf.get(_stem(right), 1.0))
    for left, right in stem_bigrams:
        if f"{left} {right}" in normalized_stems:
            score += 0.5 * (idf.get(left, 1.0) + idf.get(right, 1.0))
    if normalized_query and normalized_query in normalized:
        score += 2.0
    result_cues = sum(
        token
        in {
            "conclusion",
            "conclusions",
            "result",
            "results",
            "significant",
            "respectively",
            "associated",
            "association",
        }
        for token in tokens
    )
    score += 0.4 * result_cues
    score += 0.35 * sum(token.isdigit() for token in tokens)
    for term in content_terms:
        if term.isdigit() and stems.get(term, 0):
            score += 1.5
    return score / (1.0 + 0.1 * math.log(1.0 + len(tokens)))


def _address_key(address: MemoryEntryAddress) -> tuple[str, str, int, str, str]:
    return (
        address.memory.scope_id,
        address.memory.artifact.artifact_id,
        address.memory.artifact.revision,
        address.entry_id,
        address.entry_version_id,
    )


def _fragment_item(fragment: PreparedFragment) -> dict[str, object]:
    memory = fragment.address.memory
    return {
        "citation": {
            "scope_id": memory.scope_id,
            "memory_ref": memory.artifact.model_dump(mode="json"),
            "entry_id": fragment.address.entry_id,
            "entry_version_id": fragment.address.entry_version_id,
        },
        "content": fragment.text,
        "start": fragment.start,
        "end": fragment.end,
    }


def _render_fragments(fragments: Sequence[PreparedFragment]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_fragment_item(fragment) for fragment in fragments],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _fragments_bytes(fragments: Sequence[PreparedFragment]) -> int:
    return len(_render_fragments(fragments).encode("utf-8"))


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(entries: Sequence[_PreparedContextEntry]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry) for entry in entries],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": entry.citation,
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _rendered_bytes(entries: Sequence[_PreparedContextEntry]) -> int:
    return len(_render(entries).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"
