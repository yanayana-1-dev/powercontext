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
import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    PDFPageSource,
    parse_pdf_document,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

SHA = "a" * 64


def _blank_pdf(pages: int = 2) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_blank_pages_are_returned_in_order_and_revisions_change_identity() -> None:
    content = _blank_pdf(2)
    pages = parse_pdf_document(PDFDocumentInput(document_id="control", content=content))
    assert [(page.page_number, page.text) for page in pages] == [(1, ""), (2, "")]
    assert all(page.document_sha256 == hashlib.sha256(content).hexdigest() for page in pages)

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.add_metadata({"/Title": "New revision"})
    buffer = io.BytesIO()
    writer.write(buffer)
    revised = parse_pdf_document(PDFDocumentInput(document_id="control", content=buffer.getvalue()))

    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    assert asyncio.run(registry.resolve(pages[0])).name != asyncio.run(registry.resolve(revised[0])).name


def test_empty_password_permission_marker_is_readable() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.encrypt(user_password="", owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)

    pages = parse_pdf_document(PDFDocumentInput(document_id="permissions", content=buffer.getvalue()))
    assert len(pages) == 1 and pages[0].page_number == 1


@pytest.mark.parametrize("raw", [b"", b"not a PDF", b"%PDF-1.7\ntruncated"])
def test_corrupt_documents_fail_atomically(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="invalid", content=raw))


def test_page_source_reads_back_and_projects_text_evidence() -> None:
    page = PDFPageInput(document_id="report", document_sha256=SHA, page_number=7, text="net revenue 2022")
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(page))

    assert isinstance(source, PDFPageSource)
    assert source.source_id == source.name
    assert asyncio.run(registry.read(source)) == page
    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "pdf-page"
    assert evidence.content == "report\nnet revenue 2022"
    assert evidence.metadata == {"document_id": "report", "document_sha256": SHA, "page_number": 7}


class _PagePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="page",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"].strip(),
                sources=(source,),
            )
            for source in request.sources
        )


def test_search_returns_distinct_exact_page_citations(tmp_path: Path) -> None:
    pages = [
        PDFPageInput(document_id="alpha", document_sha256=SHA, page_number=1, text="net revenue grew in 2022"),
        PDFPageInput(document_id="alpha", document_sha256=SHA, page_number=2, text="Item 3. Legal Proceedings"),
        PDFPageInput(document_id="alpha", document_sha256=SHA, page_number=3, text=""),
    ]
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    observations = [project_source_for_transport(registry, asyncio.run(registry.resolve(page))) for page in pages]
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))

    async def scenario() -> None:
        async with open_builtin_runtime(config) as runtime:
            scope = await runtime.scopes.create(ScopeDraft(title="PDF", summary="Pages", idempotency_key="pdf"))
            await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
            for observation in observations:
                await runtime.ingestion.submit(
                    SubmitSourceObservation(scope_id=scope.scope_id, observation=observation)
                )
        async with open_builtin_runtime(config, candidate_pipeline=_PagePipeline()) as runtime:
            memory = runtime.memory.for_scope(scope.scope_id)
            for _ in range(3):
                if not (await memory.flush()).processed:
                    break
            assert await search_pdf_pages(memory, "zebra", limit=5) == ()
            assert await search_pdf_pages(memory, "legal proceedings", limit=0) == ()
            citations = await search_pdf_pages(memory, "legal proceedings", limit=5)
            assert 0 < len(citations) <= 5
            seen: set[str] = set()
            for citation in citations:
                entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
                assert len(entry.sources) == 1
                assert entry.sources[0].source_type == "pdf-page"
                assert entry.sources[0].source_id not in seen
                seen.add(entry.sources[0].source_id)
            assert citations[0].entry_id

    asyncio.run(scenario())
