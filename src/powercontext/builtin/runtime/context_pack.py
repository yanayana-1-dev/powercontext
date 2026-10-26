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

"""Incremental Context Pack wire protocol.

A full pack carries the deterministic UTF-8 chunks of one prepared context.
A delta reuses a valid prefix of an existing pack and appends the target's
remaining chunks, so a receiver only needs the serialized pack and delta to
rebuild exactly the same bytes and digest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ContextPackError(ValueError):
    """Raised when a Context Pack or delta violates the wire protocol."""


class ContextPack:
    """One complete context represented as deterministic UTF-8 chunks."""

    def __init__(self, items: tuple[bytes, ...]) -> None:
        normalized = _normalize_items(items)
        digest = _pack_digest(normalized)
        size = 0
        for _ in range(10):
            data = _pack_bytes(normalized, digest, size)
            if len(data) == size:
                break
            size = len(data)
        else:
            raise ContextPackError("could not stabilize context pack byte count")  # noqa: TRY003
        self.items = normalized
        self.digest = digest
        self.byte_count = size

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        """Build a complete pack from explicit byte chunks."""

        return cls(items)

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = 512) -> ContextPack:
        """Deterministically chunk content while keeping every chunk valid UTF-8."""

        if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")  # noqa: TRY003
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("content must be valid UTF-8") from error  # noqa: TRY003
        elif isinstance(content, str):
            text = content
        else:
            raise ContextPackError("content must be str or bytes")  # noqa: TRY003
        return cls.build(_chunk_text(text, chunk_bytes))

    def to_bytes(self) -> bytes:
        """Serialize the canonical v1 pack."""

        return _pack_bytes(self.items, self.digest, self.byte_count)

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        """Decode and validate one canonical or equivalent JSON pack."""

        payload = _decode_json(raw)
        if set(payload) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("context pack must contain exactly the v1 fields")  # noqa: TRY003
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("unexpected context pack schema")  # noqa: TRY003
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ContextPackError("context pack digest must be sha256:<lower hex>")  # noqa: TRY003
        byte_count = payload["byte_count"]
        if not isinstance(byte_count, int) or isinstance(byte_count, bool):
            raise ContextPackError("context pack byte_count must be an integer")  # noqa: TRY003
        pack = cls(items)
        if pack.digest != digest or pack.byte_count != byte_count:
            raise ContextPackError("context pack digest or byte count does not match")  # noqa: TRY003
        return pack

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ContextPack) and self.items == other.items

    def __hash__(self) -> int:
        return hash((PACK_SCHEMA, self.items))


class ContextPackDelta:
    """One target pack expressed as a reusable base prefix plus appended chunks."""

    def __init__(
        self,
        base_digest: str,
        target_digest: str,
        prefix_items: int,
        appended: tuple[bytes, ...],
    ) -> None:
        self.base_digest = base_digest
        self.target_digest = target_digest
        self.prefix_items = prefix_items
        self.appended = tuple(appended)

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Take the longest shared chunk prefix and carry the target suffix."""

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta endpoints must be ContextPacks")  # noqa: TRY003
        prefix = 0
        common = min(len(base.items), len(target.items))
        while prefix < common and base.items[prefix] == target.items[prefix]:
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def to_bytes(self) -> bytes:
        """Serialize the canonical v1 delta without a byte_count wire field."""

        return _canonical({
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [_encode_item(item) for item in self.appended],
        })

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        """Decode and validate one JSON delta."""

        payload = _decode_json(raw)
        if set(payload) != {"schema", "base_digest", "target_digest", "prefix_items", "appended"}:
            raise ContextPackError("context pack delta must contain exactly the v1 fields")  # noqa: TRY003
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("unexpected context pack delta schema")  # noqa: TRY003
        for field in ("base_digest", "target_digest"):
            value = payload[field]
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ContextPackError(f"{field} must be sha256:<lower hex>")  # noqa: TRY003
        prefix = payload["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")  # noqa: TRY003
        return cls(
            payload["base_digest"],
            payload["target_digest"],
            prefix,
            _decode_items(payload["appended"]),
        )

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, ContextPackDelta)
            and self.base_digest == other.base_digest
            and self.target_digest == other.target_digest
            and self.prefix_items == other.prefix_items
            and self.appended == other.appended
        )

    def __hash__(self) -> int:
        return hash((self.base_digest, self.target_digest, self.prefix_items, self.appended))


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate a delta against its base and rebuild the target pack."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply requires a ContextPack and a ContextPackDelta")  # noqa: TRY003
    if delta.base_digest != base.digest:
        raise ContextPackError("context pack delta base digest does not match")  # noqa: TRY003
    prefix = delta.prefix_items
    if not isinstance(prefix, int) or isinstance(prefix, bool) or not 0 <= prefix <= len(base.items):
        raise ContextPackError("prefix_items is outside the base pack")  # noqa: TRY003
    if any(not isinstance(item, bytes) for item in delta.appended):
        raise ContextPackError("context pack delta appended chunks must be bytes")  # noqa: TRY003
    target = ContextPack.build(base.items[:prefix] + delta.appended)
    if target.digest != delta.target_digest:
        raise ContextPackError("context pack delta target digest does not match")  # noqa: TRY003
    return target


def _chunk_text(text: str, chunk_bytes: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    current = bytearray()
    for character in text:
        try:
            encoded = character.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContextPackError("content must be valid UTF-8") from error  # noqa: TRY003
        if len(encoded) > chunk_bytes:
            raise ContextPackError("chunk_bytes cannot form a valid UTF-8 boundary")  # noqa: TRY003
        if current and len(current) + len(encoded) > chunk_bytes:
            chunks.append(bytes(current))
            current = bytearray()
        current.extend(encoded)
    if current:
        chunks.append(bytes(current))
    return tuple(chunks)


def _normalize_items(items: tuple[bytes, ...]) -> tuple[bytes, ...]:
    normalized = tuple(items)
    if any(not isinstance(item, bytes) for item in normalized):
        raise ContextPackError("context pack items must be bytes")  # noqa: TRY003
    return normalized


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": [_encode_item(item) for item in items]}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _pack_bytes(items: tuple[bytes, ...], digest: str, byte_count: int) -> bytes:
    return _canonical({
        "schema": PACK_SCHEMA,
        "items": [_encode_item(item) for item in items],
        "digest": digest,
        "byte_count": byte_count,
    })


def _encode_item(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("context pack chunks must be a Base64 string array")  # noqa: TRY003
    items: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("context pack chunks must be Base64 strings")  # noqa: TRY003
        try:
            items.append(base64.b64decode(value, validate=True))
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("context pack chunk is not valid Base64") from error  # noqa: TRY003
    return tuple(items)


def _decode_json(raw: bytes) -> dict[str, object]:
    if not isinstance(raw, (bytes, bytearray)):
        raise ContextPackError("wire payload must be bytes")  # noqa: TRY003
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("wire payload must be UTF-8 JSON") from error  # noqa: TRY003
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContextPackError("wire payload must be valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict):
        raise ContextPackError("wire payload must be a JSON object")  # noqa: TRY003
    return payload


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
