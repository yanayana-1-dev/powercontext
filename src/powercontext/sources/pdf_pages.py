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

"""Full physical-page PDF capture with page-level evidence retrieval."""

from __future__ import annotations

import hashlib
import io
import re
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME = "pdf-page"


class PDFDocumentInput(BaseModel):
    """One complete offline PDF document awaiting page-level capture."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page extracted in document order."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """One captured PDF page preserved as exact original evidence."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    page_text: str = ""


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int, /) -> str:
    """Build the revision-stable identity of one physical page."""

    identity = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pdf page identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return identity


def _extract_pages(content: bytes) -> list[str]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            if reader.decrypt("") == 0:
                raise ValueError("PDF requires a non-empty password")  # noqa: TRY003
        pages = [(page.extract_text() or "") for page in reader.pages]
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - any parse failure must fail atomically
        raise ValueError(f"unreadable PDF document: {error}") from error
    if not pages:
        raise ValueError("PDF document contains no pages")  # noqa: TRY003
    return pages


def parse_pdf_document(value: PDFDocumentInput, /) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of the complete original PDF."""

    document_id = value.document_id
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must not be empty or blank")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    pages = _extract_pages(value.content)
    return tuple(
        PDFPageInput(
            document_id=document_id,
            document_sha256=digest,
            page_number=number,
            text=text,
        )
        for number, text in enumerate(pages, start=1)
    )


class PDFPageSourceAdapter:
    """Resolve and read page-level PDF evidence Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=identity,
            source_id=identity,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            page_text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.page_text,
        )


class PDFPageTextEvidenceProjection:
    """Expose one page as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.page_text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    projections=(PDFPageTextEvidenceProjection(),),
)


_QUESTION_STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have how in into is it its of on or that the their there these this to
    was were what when where which who whose will with would about according during each other over under
    do does did been being can could should than then them they those there's
    """.split()
)
_TERM_PATTERN = re.compile(r"[A-Za-z0-9]+")


def _query_variants(question: str) -> list[str]:
    terms = _TERM_PATTERN.findall(question)
    if not terms:
        return []
    keyword_terms = [term for term in terms if term.casefold() not in _QUESTION_STOPWORDS]
    variants = [" ".join(terms)]
    if keyword_terms and keyword_terms != terms:
        variants.insert(0, " ".join(keyword_terms))
    return variants


async def search_pdf_pages(memory, question: str, limit: int = 10) -> tuple:
    """Search one scoped Memory and return distinct page-level exact citations."""

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if limit < 1:
        raise ValueError("search limit must be at least one")  # noqa: TRY003
    variants = _query_variants(question)
    if not variants:
        return ()
    ordered: list[MemoryCitation] = []
    seen_locations: set[tuple[str, int]] = set()
    pool = max(limit * 5, 50)
    for variant in variants:
        page = await memory.search(SearchMemoryRequest(query=variant, mode="fts", limit=pool))
        for hit in page.hits:
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            locations = set()
            for reference in entry.sources:
                source_id = reference.source_id
                if not source_id.startswith("pdf:"):
                    continue
                parts = source_id.split(":")
                try:
                    page_number = int(parts[-1])
                except ValueError:
                    continue
                locations.add((parts[-2], page_number))
            if not locations:
                continue
            location = next(iter(locations))
            if location in seen_locations:
                continue
            seen_locations.add(location)
            ordered.append(citation)
            if len(ordered) == limit:
                return tuple(ordered)
    return tuple(ordered)


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
