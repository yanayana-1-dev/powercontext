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

import pytest

from powercontext.sources import (
    SourceDefinitionRegistry,
    SourceMaterialization,
    TextEvidence,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.csv_rows import (
    CSV_ROW_SOURCE_DEFINITION,
    CSVDocumentInput,
    CSVRowInput,
    CSVRowSource,
    csv_row_source_id,
    parse_csv_document,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

HEADER = "id,name,note"


def _parse(content: bytes, *, document_id: str = "airports", primary_key: str = "id") -> tuple[CSVRowInput, ...]:
    return parse_csv_document(CSVDocumentInput(document_id=document_id, primary_key=primary_key, content=content))


def test_parse_accepts_bom_crlf_multiline_quotes_and_padded_headers() -> None:
    content = b'\xef\xbb\xbf" id ",name,note\r\n001,"Airport, East","first line\r\nsecond ""quoted"" line"\r\n'
    rows = _parse(content, primary_key=" id ")

    assert len(rows) == 1
    assert rows[0].columns == (" id ", "name", "note")
    assert rows[0].values == ("001", "Airport, East", 'first line\r\nsecond "quoted" line')
    assert rows[0].primary_key_value == "001"


def test_parse_ignores_blank_physical_records_and_allows_header_only_documents() -> None:
    assert _parse(b"id,name\n\n") == ()
    assert _parse(b'id,name\n1,"a\r\nb"\n\n2,c\n')[1].values == ("2", "c")


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\n1,one\n",
        b"id,id\n1,1\n",
        b",name\n1,one\n",
        b" \t,name\n1,one\n",
        b"id,name\n,one\n",
        b"id,name\n1\n",
        b"id,name\n1,one\n1,duplicate\n",
        b'id,name\n1,"unclosed\n',
        b"id,name\n\xff,invalid utf-8\n",
    ],
)
def test_invalid_documents_fail_with_value_error(content: bytes) -> None:
    with pytest.raises(ValueError):
        _parse(content)


def test_document_and_primary_key_must_be_valid_and_exact() -> None:
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one\n", document_id=" airports ")
    with pytest.raises(ValueError):
        _parse(b"id,name\n1,one\n", primary_key=" ")
    with pytest.raises(ValueError):
        _parse(b'" id ",name\n1,one\n', primary_key="id")


def test_identity_is_delimiter_safe_and_bounded() -> None:
    assert csv_row_source_id("doc:one", "key/one") == "csv:doc%3Aone:key%2Fone"
    assert csv_row_source_id("doc:one", "key/one") != csv_row_source_id("doc:two", "key/one")
    assert csv_row_source_id("doc:one", "key/one") != csv_row_source_id("doc:one", "key:one")

    with pytest.raises(ValueError):
        csv_row_source_id(" doc ", "key")
    with pytest.raises(ValueError):
        csv_row_source_id("doc", "x" * 260)


def test_resolved_source_reads_back_the_original_record_and_projects_text_evidence() -> None:
    rows = _parse(b"id,name\n1,one\n2,two\n")
    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(rows[0]))

    assert isinstance(source, CSVRowSource)
    assert source.materialization is SourceMaterialization.CAPTURED
    assert source.source_id == source.name == "csv:airports:1"
    assert asyncio.run(registry.read(source)) == rows[0]

    evidence = TextEvidence.model_validate(registry.project(source, TEXT_EVIDENCE_PROJECTION_KEY))
    assert evidence.source_type == "csv-row"
    assert evidence.source_id == "csv:airports:1"
    assert evidence.content == json.dumps({"id": "1", "name": "one"}, ensure_ascii=False, separators=(",", ":"))
    assert evidence.metadata == {
        "document_id": "airports",
        "primary_key": "id",
        "primary_key_value": "1",
        "columns": ["id", "name"],
        "row": {"id": "1", "name": "one"},
    }


def test_definition_declares_the_csv_row_contract() -> None:
    assert CSV_ROW_SOURCE_DEFINITION.name == "csv-row"
    assert CSV_ROW_SOURCE_DEFINITION.version == "1"
    assert CSV_ROW_SOURCE_DEFINITION.input_class is CSVRowInput
    assert CSV_ROW_SOURCE_DEFINITION.source_class is CSVRowSource

    manifest = manifest_for_definition(CSV_ROW_SOURCE_DEFINITION)
    assert manifest.name == "csv-row"
    assert tuple(projection.key for projection in manifest.projections) == (TEXT_EVIDENCE_PROJECTION_KEY,)

    registry = SourceDefinitionRegistry((CSV_ROW_SOURCE_DEFINITION,))
    source = asyncio.run(registry.resolve(_parse(b"id,name\n1,one\n")[0]))
    observation = project_source_for_transport(registry, source)
    assert observation.source_type == "csv-row"
    assert observation.name == "csv:airports:1"
