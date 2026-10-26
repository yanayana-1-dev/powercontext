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

"""Row-level Sources captured from an offline CSV document.

A CSV document is validated as a whole before any row is returned. Each data
record becomes one ``CAPTURED`` Source whose identity is derived from the
document id and the value of the configured primary-key column, so reordering
the file never changes a row's identity. The standard ``text-evidence``
projection exposes the original record without coercing any field value.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

CSV_ROW_SOURCE_NAME = "csv-row"
CSV_ROW_SOURCE_VERSION = "1"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV document selected by a primary-key column."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One parsed CSV record, independent of where the document was stored."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV row addressed by its document and primary-key value."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]

    @model_validator(mode="before")
    @classmethod
    def derive_identity(cls, data: Any) -> Any:
        """Fill the row identity from its components when callers omit it."""

        if not isinstance(data, Mapping):
            return data
        fields = dict(data)
        source_id = fields.get("source_id")
        if source_id is None and fields.get("document_id") is not None and fields.get("primary_key_value") is not None:
            source_id = csv_row_source_id(fields["document_id"], fields["primary_key_value"])
            fields["source_id"] = source_id
        if fields.get("name") is None and isinstance(source_id, str):
            fields["name"] = source_id
        return fields

    @model_validator(mode="after")
    def validate_identity(self) -> CSVRowSource:
        if not self.source_id or not self.source_id.strip():
            raise ValueError("source_id must be a non-empty string")  # noqa: TRY003
        if self.source_id != self.source_id.strip():
            raise ValueError("source_id must not contain leading or trailing whitespace")  # noqa: TRY003
        if self.name != self.source_id:
            raise ValueError("name must equal source_id")  # noqa: TRY003
        if len(self.source_id) > MAX_SOURCE_ID_LENGTH:
            raise ValueError(f"source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
        if len(self.columns) != len(self.values):
            raise ValueError("columns and values must have the same length")  # noqa: TRY003
        return self


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and validate one whole CSV document into row inputs.

    Every error in the document, whether it is an encoding, CSV quoting or data
    validation problem, is reported as ``ValueError`` before any row escapes.
    """

    _require_document_id(value.document_id)
    primary_key = value.primary_key
    if not isinstance(primary_key, str) or not primary_key.strip():
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    records = _read_records(_decode_document(value.content))
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003

    header = records[0]
    _validate_header(header)
    if primary_key not in header:
        raise ValueError(f"primary_key {primary_key!r} does not name a CSV header field")  # noqa: TRY003

    return _parse_rows(value.document_id, primary_key, header, records[1:])


def _decode_document(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003


def _parse_rows(
    document_id: str,
    primary_key: str,
    header: list[str],
    records: list[list[str]],
) -> tuple[CSVRowInput, ...]:
    key_index = header.index(primary_key)
    columns = tuple(header)
    rows: list[CSVRowInput] = []
    primary_key_values: set[str] = set()

    for position, record in enumerate(records, start=2):
        # ``csv.reader`` yields an empty list for a completely empty physical record.
        if not record:
            continue
        if len(record) != len(header):
            raise ValueError(f"CSV record {position} has {len(record)} fields, expected {len(header)}")  # noqa: TRY003
        primary_key_value = record[key_index]
        if not primary_key_value.strip():
            raise ValueError(f"CSV record {position} has a blank primary-key value")  # noqa: TRY003
        if primary_key_value in primary_key_values:
            raise ValueError(f"duplicate primary-key value {primary_key_value!r}")  # noqa: TRY003
        primary_key_values.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=document_id,
                primary_key=primary_key,
                primary_key_value=primary_key_value,
                columns=columns,
                values=tuple(record),
            )
        )

    return tuple(rows)


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Return the delimiter-safe identity of one CSV row."""

    _require_document_id(document_id)
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"CSV row identity must not exceed {MAX_SOURCE_ID_LENGTH} characters")  # noqa: TRY003
    return source_id


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


class CSVRowSourceAdapter:
    """Resolve and read row-level CSV Sources."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        return CSVRowSource(
            source_id=csv_row_source_id(value.document_id, value.primary_key_value),
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION: SourceDefinition[CSVRowInput, CSVRowSource, CSVRowInput] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version=CSV_ROW_SOURCE_VERSION,
    projections=(CSVRowTextEvidenceProjection(),),
)


def _require_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError("document_id must not contain leading or trailing whitespace")  # noqa: TRY003


def _read_records(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise ValueError(f"CSV document is malformed: {error}") from error  # noqa: TRY003


def _validate_header(header: list[str]) -> None:
    if not header:
        raise ValueError("CSV header record must not be empty")  # noqa: TRY003
    seen: set[str] = set()
    for field in header:
        if not field.strip():
            raise ValueError("CSV header fields must be non-empty and not blank")  # noqa: TRY003
        if field in seen:
            raise ValueError(f"duplicate CSV header field {field!r}")  # noqa: TRY003
        seen.add(field)


__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSV_ROW_SOURCE_NAME",
    "CSV_ROW_SOURCE_VERSION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "CSVRowSourceAdapter",
    "CSVRowTextEvidenceProjection",
    "csv_row_source_id",
    "parse_csv_document",
]
