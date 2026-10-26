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

"""Context Pack wire protocol with prefix-reuse deltas and digest validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass, field

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DIGEST_PREFIX = "sha256:"
_DEFAULT_CHUNK_BYTES = 512


class ContextPackError(ValueError):
    """A Context Pack protocol or chunking violation."""


def _canonical_json(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_of_items(items: tuple[bytes, ...], /) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": [base64.b64encode(item).decode("ascii") for item in items]}
    return DIGEST_PREFIX + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _encode_items(items: tuple[bytes, ...], /) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_b64_list(values: object, /) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("items must be an array of base64 strings")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("items must contain base64 strings")
        try:
            chunk = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("items must contain valid base64") from error
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("pack items must be valid UTF-8") from error
        decoded.append(chunk)
    return tuple(decoded)


@dataclass(frozen=True)
class ContextPack:
    """One complete context payload as digest-protected ordered chunks."""

    items: tuple[bytes, ...]
    digest: str = field(default="", compare=True)

    def __post_init__(self) -> None:
        expected = _digest_of_items(self.items)
        if not self.digest:
            object.__setattr__(self, "digest", expected)
        elif self.digest != expected:
            raise ContextPackError("pack digest does not match its items")

    @classmethod
    def build(cls, items: tuple[bytes, ...], /) -> ContextPack:
        """Construct one complete pack from ordered chunks."""

        if not isinstance(items, tuple) or any(not isinstance(item, bytes) for item in items):
            raise ContextPackError("pack items must be a tuple of bytes")
        return cls(items=items)

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk arbitrary UTF-8 text deterministically without altering bytes."""

        if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            raise ContextPackError("content must be str or bytes")
        items: list[bytes] = []
        start = 0
        while start < len(data):
            end = min(start + chunk_bytes, len(data))
            while end < len(data) and end > start and (data[end] & 0xC0) == 0x80:
                end -= 1
            if end == start:
                raise ContextPackError("chunk_bytes cannot fit one valid UTF-8 character")
            items.append(data[start:end])
            start = end
        return cls.build(tuple(items))

    @property
    def byte_count(self, /) -> int:
        """Exact wire size of this pack, including every protocol field."""

        return len(self.to_bytes())

    def to_bytes(self, /) -> bytes:
        """Serialize the complete pack with its digest and self-inclusive byte count."""

        unsigned = {"schema": PACK_SCHEMA, "items": _encode_items(self.items)}
        count = 0
        for _ in range(16):
            payload = {**unsigned, "digest": self.digest, "byte_count": count}
            encoded = _canonical_json(payload)
            if len(encoded) == count:
                return encoded
            count = len(encoded)
        raise ContextPackError("byte count did not converge")  # pragma: no cover

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPack:
        """Decode and fully validate one serialized complete pack."""

        payload = _decode_json_object(data)
        _require_exact_fields(payload, {"schema", "items", "digest", "byte_count"}, "context pack")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError("unknown context pack schema")
        if not isinstance(payload["digest"], str):
            raise ContextPackError("digest must be a string")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int:
            raise ContextPackError("byte_count must be an integer")
        pack = cls(items=_decode_b64_list(payload["items"]), digest=payload["digest"])
        if pack.byte_count != byte_count:
            raise ContextPackError("byte_count does not match the serialized pack")
        return pack


def _decode_json_object(data: bytes, /) -> dict:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("payload must be canonical JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError("payload must be a JSON object")
    return payload


def _require_exact_fields(payload: dict, expected: set[str], label: str, /) -> None:
    observed = set(payload)
    missing = expected - observed
    extra = observed - expected
    if missing or extra:
        raise ContextPackError(f"{label} fields do not match the protocol: missing={sorted(missing)} extra={sorted(extra)}")


@dataclass(frozen=True)
class ContextPackDelta:
    """One prefix-reuse delta carrying the target suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...] = ()

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Select the common prefix of two packs and carry the target suffix."""

        common = 0
        for left, right in zip(base.items, target.items):
            if left != right:
                break
            common += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=common,
            appended=target.items[common:],
        )

    @property
    def byte_count(self, /) -> int:
        """Exact wire size of this delta."""

        return len(self.to_bytes())

    def to_bytes(self, /) -> bytes:
        """Serialize the delta with its protocol fields."""

        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        }
        return _canonical_json(payload)

    @classmethod
    def from_bytes(cls, data: bytes, /) -> ContextPackDelta:
        """Decode and validate one serialized delta."""

        payload = _decode_json_object(data)
        _require_exact_fields(
            payload,
            {"schema", "base_digest", "target_digest", "prefix_items", "appended"},
            "context pack delta",
        )
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError("unknown context pack delta schema")
        for field_name in ("base_digest", "target_digest"):
            if not isinstance(payload[field_name], str):
                raise ContextPackError(f"{field_name} must be a string")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        return cls(
            base_digest=payload["base_digest"],
            target_digest=payload["target_digest"],
            prefix_items=prefix,
            appended=_decode_b64_list(payload["appended"]),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Validate one delta against its base pack and rebuild the target."""

    if base.digest != delta.base_digest:
        raise ContextPackError("delta base digest does not match the provided pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack")
    rebuilt = base.items[: delta.prefix_items] + delta.appended
    target = ContextPack.build(rebuilt)
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack digest does not match the delta target")
    return target


__all__ = [
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
