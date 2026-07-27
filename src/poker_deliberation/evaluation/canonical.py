"""Dedicated canonical JSON and hash domains for offline evaluation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.evaluation.models import (
    EVALUATION_CANONICALIZATION,
    EVALUATION_SCHEMA_VERSION,
)

DATASET_CONTENT_DOMAIN = "poker-offline-evaluation-dataset-content-v1"
EVALUATION_RESULT_DOMAIN = "poker-offline-evaluation-result-v1"
SCORER_CONFIG_DOMAIN = "poker-offline-evaluation-scorer-config-v1"
SOURCE_CONFIG_DOMAIN = "poker-offline-evaluation-source-config-v1"
TOOL_CONTRACT_DOMAIN = "poker-offline-evaluation-tool-contract-v1"
TOOL_INPUT_DOMAIN = "poker-offline-evaluation-tool-input-v1"
TOOL_OUTPUT_DOMAIN = "poker-offline-evaluation-tool-output-v1"

T = TypeVar("T", bound=BaseModel)


class CanonicalEvaluationError(ValueError):
    """A stable non-secret canonical evaluation validation failure."""


def _require_nfc(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalEvaluationError("canonical evaluation text must be NFC")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        normalized: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalEvaluationError("canonical evaluation keys must be strings")
            normalized_key = _require_nfc(key)
            if normalized_key in normalized:
                raise CanonicalEvaluationError("duplicate canonical evaluation key")
            normalized.add(normalized_key)
            result[normalized_key] = _jsonable(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return _require_nfc(value)
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        raise CanonicalEvaluationError("canonical evaluation numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise CanonicalEvaluationError("value is not canonical evaluation JSON")


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
        if isinstance(exc, CanonicalEvaluationError):
            raise
        raise CanonicalEvaluationError("value is not canonical evaluation JSON") from exc


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        normalized_key = _require_nfc(key)
        if normalized_key in normalized:
            raise CanonicalEvaluationError("duplicate canonical evaluation JSON key")
        normalized.add(normalized_key)
        result[normalized_key] = value
    return result


def parse_canonical_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalEvaluationError("canonical evaluation JSON cannot contain a BOM")
    if data.endswith((b"\n", b"\r")):
        raise CanonicalEvaluationError(
            "canonical evaluation JSON cannot contain a trailing newline"
        )
    try:
        text = data.decode("utf-8", errors="strict")
        _require_nfc(text)
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalEvaluationError(f"non-finite canonical evaluation number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalEvaluationError("invalid canonical evaluation JSON") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalEvaluationError("evaluation JSON bytes are not canonical")
    return value


def parse_canonical_model(data: bytes, model: type[T]) -> T:
    raw = parse_canonical_json(data)
    if not isinstance(raw, dict):
        raise CanonicalEvaluationError("canonical evaluation model must be a JSON object")
    if raw.get("schema_version") != EVALUATION_SCHEMA_VERSION:
        raise CanonicalEvaluationError("unsupported evaluation schema version")
    if raw.get("canonicalization") != EVALUATION_CANONICALIZATION:
        raise CanonicalEvaluationError("unsupported evaluation canonicalization")
    try:
        value = TypeAdapter(model).validate_json(data, strict=True)
    except ValidationError as exc:
        raise CanonicalEvaluationError(
            "canonical evaluation JSON violates its strict schema"
        ) from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalEvaluationError("strict evaluation model bytes mismatch")
    return value


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def domain_sha256(domain: str, data: bytes) -> str:
    try:
        prefix = domain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalEvaluationError("evaluation hash domain must be ASCII") from exc
    return sha256_bytes(prefix + b"\0" + data)


def canonical_domain_sha256(domain: str, value: Any) -> str:
    return domain_sha256(domain, canonical_json_bytes(value))


__all__ = [
    "DATASET_CONTENT_DOMAIN",
    "EVALUATION_RESULT_DOMAIN",
    "SCORER_CONFIG_DOMAIN",
    "SOURCE_CONFIG_DOMAIN",
    "TOOL_CONTRACT_DOMAIN",
    "TOOL_INPUT_DOMAIN",
    "TOOL_OUTPUT_DOMAIN",
    "CanonicalEvaluationError",
    "canonical_domain_sha256",
    "canonical_json_bytes",
    "domain_sha256",
    "parse_canonical_json",
    "parse_canonical_model",
    "sha256_bytes",
]
