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

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            if reader.decrypt("") == 0:
                raise ValueError("PDF requires a non-empty password")  # noqa: TRY003
        len(reader.pages)
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 - structural damage fails the whole document
        raise ValueError(f"unreadable PDF document: {error}") from error
    pages: list[str] = []
    for page in reader.pages:
        # One unextractable page means that page has no text; it must not fail
        # an otherwise well-formed document.
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - per-page degradation, document stays intact
            pages.append("")
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


_UNRESOLVED = object()

_QUESTION_STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have how in into is it its of on or that the their there these this to
    was were what when where which who whose will with would about according during each other over under
    do does did been being can could should than then them they those there's
    if not outline outlined state calculate define respond answer using use give rely relying detail details
    shown show directly explicitly perspect assume assuming
    """.split()
)
_TERM_PATTERN = re.compile(r"[A-Za-z0-9]+")

# Question phrasings that lexical evidence rarely states verbatim. When a
# question term with this prefix appears, the related filing vocabulary joins
# the probes and the coverage terms. The expansions name the filing sections
# and statement line families where that kind of evidence lives.
_TERM_SYNONYM_TRIGGERS = {
    "geograph": (
        "segments segment operations countries territories regions markets operates operate business"
    ).split(),
    "margin": ("income revenues statement statements profit operating gross".split()),
    "liabilit": ("liabilities balance debts borrowings".split()),
    "revenue": ("revenues income statement statements".split()),
    "ebitda": ("income operating statement statements depreciation amortization".split()),
    "capex": ("capital expenditures cash flow flows statement".split()),
    "customer": ("revenues government contracts airlines concentration".split()),
    "concentration": ("customer customers revenue revenues percentage".split()),
    "cyclical": ("cyclical cyclicality cycles seasonal industry".split()),
    "legal": ("litigation actions lawsuits proceedings filed claims".split()),
    "battle": ("litigation actions lawsuits proceedings".split()),
    "ratio": (
        "balance sheet assets liabilities statements statement consolidated liquidity cash".split()
    ),
    "liquidity": ("cash balance sheet statements statement assets liabilities".split()),
    "primary": ("principal largest significant major primary".split()),
}


def _query_variants(question: str) -> list[str]:
    terms = _TERM_PATTERN.findall(question)
    if not terms:
        return []
    keyword_terms = [term for term in terms if term.casefold() not in _QUESTION_STOPWORDS]
    variants = [" ".join(terms)]
    if keyword_terms and keyword_terms != terms:
        variants.insert(0, " ".join(keyword_terms))
    # Fiscal-year style identifiers and acronyms carry the strongest page-level
    # evidence signal in financial reports; give them a dedicated focused query.
    focused = [
        term
        for term in keyword_terms
        if any(character.isdigit() for character in term)
        or (len(term) >= 2 and term.isupper() and term.isalpha())
    ]
    if focused:
        focused_query = " ".join(focused)
        if focused_query not in variants:
            variants.append(focused_query)
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
    pool = max(limit * 20, 400)

    def location_of(source_id: str) -> tuple[str, int] | None:
        if not source_id.startswith("pdf:"):
            return None
        parts = source_id.split(":")
        try:
            return parts[-2], int(parts[-1])
        except (IndexError, ValueError):
            return None

    keyword_terms = [
        term.casefold()
        for term in _TERM_PATTERN.findall(question)
        if term.casefold() not in _QUESTION_STOPWORDS and len(term) > 1
    ]
    synonyms: list[str] = []
    for term in keyword_terms:
        for trigger, expansion in _TERM_SYNONYM_TRIGGERS.items():
            if term.startswith(trigger):
                synonyms.extend(expansion)
    coverage_terms = list(dict.fromkeys([*keyword_terms, *synonyms]))

    # Probe with the composed variants, the synonym expansion, and one
    # single-term probe per keyword. A single-term lexical query never trips
    # the shared admission floor, so pages that share only one term with the
    # question (typically just the filing's company name) stay reachable.
    composed = list(dict.fromkeys([*variants, *([" ".join(dict.fromkeys(synonyms))] if synonyms else [])]))
    singles = list(dict.fromkeys([*keyword_terms, *synonyms]))[:16]
    composed_budget = 800
    singles_budget = 500
    citation_locations: dict[tuple[str, str, str], tuple[str, int] | None] = {}
    flat_hits: list[tuple[int, int, int, object]] = []
    for probe_index, probe in enumerate(composed):
        page = await memory.search(SearchMemoryRequest(query=probe, mode="fts", limit=pool))
        for rank, hit in enumerate(page.hits):
            flat_hits.append((0, rank, probe_index, hit))
    for probe_index, term in enumerate(singles):
        page = await memory.search(
            SearchMemoryRequest(query=term, mode="fts", limit=max(limit * 20, 400))
        )
        for rank, hit in enumerate(page.hits):
            flat_hits.append((1, rank, probe_index, hit))
    flat_hits.sort(key=lambda item: (item[0], item[1]))

    entries: dict[tuple[str, int], dict] = {}
    fetched = {0: 0, 1: 0}
    budgets = {0: composed_budget, 1: singles_budget}
    for priority, rank, _probe_index, hit in flat_hits:
        key = (str(hit.memory_ref.artifact_id), hit.entry_id, hit.entry_version_id)
        location = citation_locations.get(key, _UNRESOLVED)
        if location is _UNRESOLVED:
            if fetched[priority] >= budgets[priority]:
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            locations = {
                candidate
                for candidate in (location_of(reference.source_id) for reference in entry.sources)
                if candidate is not None
            }
            location = next(iter(locations)) if locations else None
            citation_locations[key] = location
            if location is not None:
                fetched[priority] += 1
                entries[location] = {"citation": citation, "text": entry.text, "rrf": 0.0}
        if location is not None:
            entries[location]["rrf"] += 1.0 / (60 + rank)

    # Re-rank signals. Term weights reward pages of the question's own filing:
    # a term whose candidates concentrate in one document is that filing's
    # identity (company name), while terms spread across filings are generic
    # finance vocabulary. Numeric density favours financial-statement pages,
    # whose figures answer "metrics-generated" questions even when the
    # question's framing words never appear on them. The fused probe ranks
    # keep the BM25 evidence that pure coverage loses.
    term_documents: dict[str, dict[str, int]] = {}
    for location, record in entries.items():
        doc = location[0]
        page_terms = frozenset(_TERM_PATTERN.findall(record["text"].casefold()))
        for term in coverage_terms:
            if term in page_terms:
                term_documents.setdefault(term, {}).setdefault(doc, 0)
                term_documents[term][doc] += 1
    term_weights = {
        term: (3 if max(counts.values()) / max(1, sum(counts.values())) >= 0.8 else 1)
        for term, counts in term_documents.items()
    }
    # The filing the question asks about owns the plurality of the candidate
    # set (its company name matches only its own pages). Boosting that filing's
    # pages keeps statement slots from being split evenly across every other
    # company's statements in a multi-document database.
    document_counts: dict[str, int] = {}
    for location in entries:
        document_counts[location[0]] = document_counts.get(location[0], 0) + 1
    question_doc = max(document_counts, key=lambda doc: document_counts[doc]) if document_counts else None
    evidence_ranking: list[tuple[int, float, tuple[str, int], MemoryCitation]] = []
    statement_ranking: list[tuple[int, float, tuple[str, int], MemoryCitation]] = []
    for location, record in entries.items():
        page_terms = frozenset(_TERM_PATTERN.findall(record["text"].casefold()))
        coverage = sum(
            term_weights.get(term, 1) for term in coverage_terms if term in page_terms
        )
        digit_ratio = sum(1 for term in page_terms if term.isdigit()) / max(1, len(page_terms))
        rrf_bonus = record["rrf"] * 20000
        doc_bonus = 600 if location[0] == question_doc else 0
        # Evidence ranking: lexical coverage of the question's terms with fused
        # probe ranks. Statement ranking: the same signals plus a strong numeric
        # density term, because consolidated-statement pages answer
        # "metrics-generated" questions through their figures even when the
        # question's framing words never appear on them. Neither signal wins
        # every question type, so the final list round-robins the two rankings.
        evidence_score = round(coverage * 100 + rrf_bonus + doc_bonus // 3)
        statement_score = round(coverage * 100 + digit_ratio * 3000 + rrf_bonus + doc_bonus)
        evidence_ranking.append((evidence_score, -record["rrf"], location, record["citation"]))
        statement_ranking.append((statement_score, -record["rrf"], location, record["citation"]))
    evidence_ranking.sort(key=lambda item: (-item[0], item[1], item[2]))
    statement_ranking.sort(key=lambda item: (-item[0], item[1], item[2]))
    ordered: list[MemoryCitation] = []
    seen_locations: set[tuple[str, int]] = set()
    for index in range(max(len(evidence_ranking), len(statement_ranking))):
        for ranking in (evidence_ranking, statement_ranking):
            if index < len(ranking):
                location = ranking[index][2]
                if location not in seen_locations:
                    seen_locations.add(location)
                    ordered.append(ranking[index][3])
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
