"""Dedicated canonical JSON and hashes for the P2-025A conformance family."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.runtime_conformance.models import (
    CONFORMANCE_CANONICALIZATION,
    CONFORMANCE_SCHEMA_VERSION,
    ConformanceRecordV1,
    RuntimeInventoryV1,
)

CONFORMANCE_RECORD_DOMAIN = "poker-runtime-conformance-record-v1"
RUNTIME_INVENTORY_DOMAIN = "poker-runtime-conformance-inventory-v1"
ALLOWLIST_DOMAIN = "poker-runtime-conformance-allowlist-v1"
APPROVAL_BINDING_DOMAIN = "poker-runtime-conformance-approval-v1"
CONTEXT_REFERENCE_DOMAIN = "poker-runtime-conformance-context-v1"

T = TypeVar("T", bound=BaseModel)


class CanonicalConformanceError(ValueError):
    """A stable non-secret canonical conformance validation failure."""


@dataclass(frozen=True, slots=True)
class CanonicalDomainDescription:
    domain: str
    schema_version: str
    canonicalization: str
    supported_types: tuple[str, ...]
    datetime_rule: str
    nfc_rule: str
    parser: str
    consumers: tuple[str, ...]


CANONICAL_DOMAIN_INVENTORY = (
    CanonicalDomainDescription(
        domain=CONFORMANCE_RECORD_DOMAIN,
        schema_version=CONFORMANCE_SCHEMA_VERSION,
        canonicalization=CONFORMANCE_CANONICALIZATION,
        supported_types=("ConformanceRecordV1",),
        datetime_rule="timezone-aware UTC encoded with Z; non-UTC rejected",
        nfc_rule="all strings and keys must already be NFC",
        parser="strict UTF-8; no BOM/newline/duplicate keys/unknown fields or versions",
        consumers=(
            "runtime conformance fixture verifier",
            "offline Python product projection verifier",
        ),
    ),
    CanonicalDomainDescription(
        domain=RUNTIME_INVENTORY_DOMAIN,
        schema_version=CONFORMANCE_SCHEMA_VERSION,
        canonicalization=CONFORMANCE_CANONICALIZATION,
        supported_types=("RuntimeInventoryV1",),
        datetime_rule="no datetime fields",
        nfc_rule="all strings and keys must already be NFC",
        parser="strict UTF-8; no BOM/newline/duplicate keys/unknown fields or versions",
        consumers=("runtime role inventory verifier",),
    ),
)


def _require_nfc(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalConformanceError("canonical conformance text must be NFC")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, datetime):
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise CanonicalConformanceError("canonical conformance datetime must be UTC")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        normalized: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalConformanceError("canonical conformance keys must be strings")
            normalized_key = _require_nfc(key)
            if normalized_key in normalized:
                raise CanonicalConformanceError("duplicate canonical conformance key")
            normalized.add(normalized_key)
            result[normalized_key] = _jsonable(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return _require_nfc(value)
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        raise CanonicalConformanceError("canonical conformance numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise CanonicalConformanceError("value is not canonical conformance JSON")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CanonicalConformanceError):
            raise
        raise CanonicalConformanceError("value is not canonical conformance JSON") from exc


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        normalized_key = _require_nfc(key)
        if normalized_key in normalized:
            raise CanonicalConformanceError("duplicate canonical conformance JSON key")
        normalized.add(normalized_key)
        result[normalized_key] = value
    return result


def parse_canonical_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalConformanceError("canonical conformance JSON cannot contain a BOM")
    if data.endswith((b"\n", b"\r")):
        raise CanonicalConformanceError(
            "canonical conformance JSON cannot contain a trailing newline"
        )
    try:
        text = data.decode("utf-8", errors="strict")
        _require_nfc(text)
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalConformanceError(f"non-finite canonical conformance number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalConformanceError("invalid canonical conformance JSON") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalConformanceError("conformance JSON bytes are not canonical")
    return value


def parse_canonical_model(data: bytes, model: type[T]) -> T:
    raw = parse_canonical_json(data)
    if not isinstance(raw, dict):
        raise CanonicalConformanceError("canonical conformance model must be a JSON object")
    if raw.get("schema_version") != CONFORMANCE_SCHEMA_VERSION:
        raise CanonicalConformanceError("unsupported conformance schema version")
    if raw.get("canonicalization") != CONFORMANCE_CANONICALIZATION:
        raise CanonicalConformanceError("unsupported conformance canonicalization")
    try:
        value = TypeAdapter(model).validate_json(data, strict=True)
    except ValidationError as exc:
        if not exc.errors() or any(error["type"] != "datetime_type" for error in exc.errors()):
            raise CanonicalConformanceError(
                "canonical conformance JSON violates its strict schema"
            ) from exc
        try:
            value = TypeAdapter(model).validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalConformanceError(
                "canonical conformance JSON violates its strict datetime schema"
            ) from fallback_exc
    if canonical_json_bytes(value) != data:
        raise CanonicalConformanceError("strict conformance model bytes mismatch")
    return value


def parse_conformance_record(data: bytes) -> ConformanceRecordV1:
    return parse_canonical_model(data, ConformanceRecordV1)


def parse_runtime_inventory(data: bytes) -> RuntimeInventoryV1:
    return parse_canonical_model(data, RuntimeInventoryV1)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def domain_sha256(domain: str, data: bytes) -> str:
    try:
        prefix = domain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalConformanceError("conformance hash domain must be ASCII") from exc
    return sha256_bytes(prefix + b"\0" + data)


def canonical_domain_sha256(domain: str, value: Any) -> str:
    return domain_sha256(domain, canonical_json_bytes(value))


def conformance_record_sha256(record: ConformanceRecordV1) -> str:
    return canonical_domain_sha256(CONFORMANCE_RECORD_DOMAIN, record)


def runtime_inventory_sha256(inventory: RuntimeInventoryV1) -> str:
    return canonical_domain_sha256(RUNTIME_INVENTORY_DOMAIN, inventory)


__all__ = [
    "ALLOWLIST_DOMAIN",
    "APPROVAL_BINDING_DOMAIN",
    "CANONICAL_DOMAIN_INVENTORY",
    "CONFORMANCE_RECORD_DOMAIN",
    "CONTEXT_REFERENCE_DOMAIN",
    "RUNTIME_INVENTORY_DOMAIN",
    "CanonicalConformanceError",
    "CanonicalDomainDescription",
    "canonical_domain_sha256",
    "canonical_json_bytes",
    "conformance_record_sha256",
    "domain_sha256",
    "parse_canonical_json",
    "parse_canonical_model",
    "parse_conformance_record",
    "parse_runtime_inventory",
    "runtime_inventory_sha256",
    "sha256_bytes",
]
