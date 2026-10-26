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

"""Page-level Sources captured from a complete offline PDF.

Every physical page is extracted with the pinned ``pypdf`` reader, including
blank pages, and becomes one immutable ``CAPTURED`` Source addressed by the
document id, the whole-file SHA-256 and the 1-based physical page number.
Search operates only through the public scoped Memory API: candidate pages are
retrieved lexically, ranked locally, and returned as exact ``MemoryCitation``
values for pages that really exist in the store.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, model_validator
from pypdf import PdfReader

from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PDF_PAGE_SOURCE_NAME = "pdf-page"
PDF_PAGE_SOURCE_VERSION = "1"
_POOL_LIMIT = 100
_QUERY_WINDOW = 6
_RRF_K = 60
_HEADING_WEIGHT = 0.1
_WINDOW_WEIGHT = 0.5
_WORD = re.compile(r"[a-z]+|\d+")
_HEADING_WORD = re.compile(r"[A-Za-z][A-Za-z-]*")
_STOPWORDS = frozenset({
    "a",
    "about",
    "above",
    "according",
    "after",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "down",
    "during",
    "each",
    "else",
    "explicitly",
    "few",
    "for",
    "from",
    "further",
    "give",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "him",
    "his",
    "how",
    "however",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "me",
    "might",
    "more",
    "most",
    "must",
    "my",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "otherwise",
    "our",
    "out",
    "over",
    "own",
    "please",
    "provide",
    "provided",
    "question",
    "rather",
    "rely",
    "relying",
    "respond",
    "response",
    "same",
    "shall",
    "she",
    "should",
    "show",
    "shown",
    "so",
    "some",
    "state",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "too",
    "under",
    "until",
    "up",
    "upon",
    "us",
    "use",
    "using",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "whose",
    "why",
    "will",
    "with",
    "within",
    "without",
    "would",
    "you",
    "your",
})


class PDFDocumentInput(BaseModel):
    """One complete PDF file and the stable id of its document."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page and its extracted text."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """A captured PDF page addressed by document, revision digest and page."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="before")
    @classmethod
    def derive_identity(cls, data: Any) -> Any:
        """Fill the page identity from its components when callers omit it."""

        if not isinstance(data, dict):
            return data
        fields = dict(data)
        source_id = fields.get("source_id")
        if source_id is None and all(isinstance(fields.get(key), str) for key in ("document_id", "document_sha256")):
            source_id = _pdf_page_source_id(
                fields["document_id"],
                fields["document_sha256"],
                fields.get("page_number"),
            )
            fields["source_id"] = source_id
        if fields.get("name") is None and isinstance(source_id, str):
            fields["name"] = source_id
        return fields

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        if not self.source_id or not self.source_id.strip():
            raise ValueError("source_id must be a non-empty string")  # noqa: TRY003
        if self.source_id != self.source_id.strip():
            raise ValueError("source_id must not contain leading or trailing whitespace")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")  # noqa: TRY003
        if len(self.source_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
        if self.page_number < 1:
            raise ValueError("page_number must be a positive physical page number")  # noqa: TRY003
        return self


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page of one complete PDF file.

    All pages are parsed before any result is returned, so a corrupt file fails
    with ``ValueError`` instead of leaking a partial page list.
    """

    document_id = value.document_id
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    content = value.content
    document_sha256 = hashlib.sha256(content).hexdigest()
    pages: list[PDFPageInput] = []
    locked = False
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            locked = not bool(reader.decrypt(""))
        if not locked:
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text()
                pages.append(
                    PDFPageInput(
                        document_id=document_id,
                        document_sha256=document_sha256,
                        page_number=page_number,
                        text=text or "",
                    )
                )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("PDF could not be parsed completely") from error  # noqa: TRY003
    if locked:
        raise ValueError("PDF requires a user password")  # noqa: TRY003
    return tuple(pages)


class PDFPageTextEvidenceProjection:
    """Expose one PDF page as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


class PDFPageSourceAdapter:
    """Resolve and read page-level PDF Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        return PDFPageSource(
            source_id=_pdf_page_source_id(value.document_id, value.document_sha256, value.page_number),
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version=PDF_PAGE_SOURCE_VERSION,
    projections=(PDFPageTextEvidenceProjection(),),
)


async def search_pdf_pages(
    memory: Any,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return up to ``limit`` distinct page citations for a natural-language question."""

    if limit < 1:
        return ()
    terms = _question_terms(question)
    if not terms:
        return ()
    channels = await _collect_candidates(memory, terms)
    if not channels:
        return ()
    return await _select_pages(memory, _rank_candidates(channels, terms), limit)


def _pdf_page_source_id(document_id: str, document_sha256: str, page_number: object) -> str:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if not isinstance(document_sha256, str) or not document_sha256:
        raise ValueError("document_sha256 must be a non-empty string")  # noqa: TRY003
    if not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")  # noqa: TRY003
    source_id = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"PDF page identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


def _question_terms(question: str) -> tuple[str, ...]:
    """Extract distinct lexical question terms without instruction boilerplate."""

    terms: list[str] = []
    seen: set[str] = set()
    for term in _WORD.findall(question.casefold()):
        if len(term) < 2 or term in _STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return tuple(terms)


def _candidate_queries(terms: tuple[str, ...]) -> tuple[str, ...]:
    if len(terms) <= _QUERY_WINDOW:
        return (" ".join(terms),)
    return tuple(" ".join(terms[index : index + _QUERY_WINDOW]) for index in range(0, len(terms), _QUERY_WINDOW))


async def _collect_candidates(
    memory: Any,
    terms: tuple[str, ...],
) -> list[tuple[float, tuple[Any, ...]]]:
    from powercontext.builtin.runtime import SearchMemoryRequest

    channels: list[tuple[float, tuple[Any, ...]]] = []
    for term in terms:
        page = await memory.search(SearchMemoryRequest(query=term, mode="fts", limit=_POOL_LIMIT))
        if not page.hits:
            continue
        channels.append((math.log(1.0 + _POOL_LIMIT / len(page.hits)), page.hits))
    for query in _candidate_queries(terms):
        page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=_POOL_LIMIT))
        if page.hits:
            channels.append((_WINDOW_WEIGHT, page.hits))
    return channels


def _rank_candidates(
    channels: list[tuple[float, tuple[Any, ...]]],
    terms: tuple[str, ...],
) -> list[Any]:
    hits: dict[str, Any] = {}
    texts: dict[str, str] = {}
    scores: dict[str, float] = {}
    for weight, channel in channels:
        for rank, hit in enumerate(channel, 1):
            hits.setdefault(hit.entry_id, hit)
            texts[hit.entry_id] = hit.text
            scores[hit.entry_id] = scores.get(hit.entry_id, 0.0) + weight / (_RRF_K + rank)
    headings = {entry_id: _heading_terms(text) for entry_id, text in texts.items()}
    document_frequency = dict.fromkeys(terms, 0)
    for values in headings.values():
        for term in terms:
            if term in values:
                document_frequency[term] += 1
    total = len(texts)
    heading_weights = {term: math.log((total + 1) / (document_frequency[term] + 1)) + 1.0 for term in terms}
    for entry_id, values in headings.items():
        bonus = sum(heading_weights[term] for term in terms if term in values)
        if bonus:
            scores[entry_id] += _HEADING_WEIGHT * bonus
    ordered = sorted(scores, key=lambda entry_id: (-scores[entry_id], entry_id))
    return [hits[entry_id] for entry_id in ordered]


async def _select_pages(memory: Any, hits: list[Any], limit: int) -> tuple[MemoryCitation, ...]:
    from powercontext.builtin.runtime import GetMemoryEntryRequest

    selected: list[MemoryCitation] = []
    seen_pages: set[tuple[str, ...]] = set()
    for hit in hits:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        if len(entry.sources) != 1 or entry.sources[0].source_type != PDF_PAGE_SOURCE_NAME:
            continue
        page_key = (entry.sources[0].source_id,)
        if page_key in seen_pages:
            continue
        seen_pages.add(page_key)
        selected.append(citation)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _heading_terms(text: str) -> frozenset[str]:
    """Return tokens that appear on short, title-like lines of one page."""

    found: set[str] = set()
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        tokens = _WORD.findall(raw.casefold())
        if not tokens or len(tokens) > 8:
            continue
        if raw.startswith("Item ") or _is_title_like(raw):
            found.update(tokens)
    return frozenset(found)


def _is_title_like(raw: str) -> bool:
    words = _HEADING_WORD.findall(raw)
    if not words:
        return False
    capitalized = sum(1 for word in words if word[0].isupper())
    return capitalized >= max(1, len(words) - 1)


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDF_PAGE_SOURCE_VERSION",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "search_pdf_pages",
]
