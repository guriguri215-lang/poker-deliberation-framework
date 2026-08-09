"""Canonical byte and hash helpers for the bounded Codex bridge."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class BridgeCanonicalError(ValueError):
    """Raised when bridge bytes are not the one accepted canonical encoding."""


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise BridgeCanonicalError("canonical datetime must be timezone-aware UTC")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise BridgeCanonicalError("canonical object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, str | int | bool | float):
        return value
    raise BridgeCanonicalError(f"unsupported canonical value: {type(value).__name__}")


def _reject_floats(value: object, path: str = "$") -> None:
    if isinstance(value, float):
        raise BridgeCanonicalError(f"floating value is forbidden at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BridgeCanonicalError(f"non-string object key at {path}")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")


def canonical_json_bytes(value: BaseModel | object) -> bytes:
    """Encode one NFC, LF, sorted-key, float-free canonical JSON value."""

    payload = _json_value(value)
    _reject_floats(payload)
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def domain_sha256(domain: str, value: bytes | BaseModel | object) -> str:
    """Hash a literal ASCII domain and exact bytes without ambiguous concatenation."""

    if not domain or not domain.isascii() or "\x00" in domain:
        raise BridgeCanonicalError("hash domain must be nonempty ASCII without NUL")
    data = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + data).hexdigest()


def parse_canonical_model(data: bytes, model: type[_ModelT]) -> _ModelT:
    """Strictly parse a model and reject alternate JSON spellings or encodings."""

    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise BridgeCanonicalError("bridge JSON must be UTF-8 without BOM and LF-only")
    try:
        value = model.model_validate_json(data, strict=True)
    except Exception as exc:
        raise BridgeCanonicalError("bridge JSON failed strict schema validation") from exc
    if canonical_json_bytes(value) != data:
        raise BridgeCanonicalError("bridge JSON bytes are noncanonical")
    return value


def without_field(value: BaseModel, field: str) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    payload.pop(field)
    return payload


__all__ = [
    "BridgeCanonicalError",
    "canonical_json_bytes",
    "domain_sha256",
    "parse_canonical_model",
    "sha256_bytes",
    "without_field",
]
