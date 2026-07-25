"""Canonical P2-027B cleanup bytes, strict parsing, and separated hashes."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Final, TypeVar

from pydantic import BaseModel, ValidationError

from poker_deliberation.local_data_cleanup_models import (
    CleanupApprovalBindingV1,
    CleanupPlanV1,
    CleanupReceiptV1,
    CleanupRootMarkerV1,
    CleanupTombstoneV1,
    TreeInventoryV1,
)

ROOT_MARKER_DOMAIN: Final = "poker-local-data-cleanup-root-marker-v1"
ROOT_IDENTITY_DOMAIN: Final = "poker-local-data-cleanup-root-identity-v1"
TREE_IDENTITY_DOMAIN: Final = "poker-local-data-cleanup-tree-identity-v1"
TREE_INVENTORY_DOMAIN: Final = "poker-local-data-cleanup-tree-inventory-v1"
ACTION_DOMAIN: Final = "poker-local-data-cleanup-action-v1"
PLAN_DOMAIN: Final = "poker-local-data-cleanup-plan-v1"
APPROVAL_BINDING_DOMAIN: Final = "poker-local-data-cleanup-approval-binding-v1"
TRANSACTION_DOMAIN: Final = "poker-local-data-cleanup-transaction-v1"
MANIFEST_DOMAIN: Final = "poker-local-data-cleanup-manifest-v1"
POINTER_DOMAIN: Final = "poker-local-data-cleanup-pointer-v1"
RECEIPT_DOMAIN: Final = "poker-local-data-cleanup-receipt-v1"
TOMBSTONE_DOMAIN: Final = "poker-local-data-cleanup-tombstone-v1"
RECONCILIATION_DOMAIN: Final = "poker-local-data-cleanup-reconciliation-v1"
RUN_ID_DOMAIN: Final = "poker-local-data-cleanup-run-id-v1"

T = TypeVar("T", bound=BaseModel)


class CleanupCanonicalError(ValueError):
    """A strict canonical cleanup value could not be admitted."""


class UnsupportedCleanupVersion(CleanupCanonicalError):
    """A cleanup value uses an unsupported schema version."""


def format_cleanup_utc(value: datetime) -> str:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise CleanupCanonicalError("cleanup datetime must be timezone-aware UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _nfc(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise CleanupCanonicalError("cleanup strings and keys must already be NFC")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return format_cleanup_utc(value)
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CleanupCanonicalError("cleanup JSON object keys must be strings")
            key = _nfc(key)
            if key in result:
                raise CleanupCanonicalError("duplicate cleanup JSON object key")
            result[key] = _jsonable(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return _nfc(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise CleanupCanonicalError("cleanup JSON numbers must be finite")
        return value
    raise CleanupCanonicalError(f"unsupported cleanup JSON value: {type(value).__name__}")


def canonical_cleanup_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CleanupCanonicalError):
            raise
        raise CleanupCanonicalError("cleanup value is not canonical JSON") from exc


def canonical_cleanup_sha256(domain: str, value: Any) -> str:
    if not domain or not domain.isascii():
        raise CleanupCanonicalError("cleanup hash domain must be nonempty ASCII")
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + canonical_cleanup_bytes(value)
    ).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        key = _nfc(key)
        if key in result:
            raise CleanupCanonicalError("duplicate cleanup JSON object key")
        result[key] = value
    return result


def parse_canonical_cleanup_json(data: bytes, *, max_bytes: int = 1_000_000) -> Any:
    if not data or len(data) > max_bytes or data.startswith(b"\xef\xbb\xbf"):
        raise CleanupCanonicalError("cleanup JSON byte envelope is invalid")
    try:
        text = data.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CleanupCanonicalError(f"invalid cleanup JSON constant: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupCanonicalError("cleanup JSON is invalid") from exc
    if canonical_cleanup_bytes(value) != data:
        raise CleanupCanonicalError("cleanup JSON bytes are not canonical")
    return value


def parse_cleanup_model(data: bytes, model: type[T], *, max_bytes: int = 1_000_000) -> T:
    raw = parse_canonical_cleanup_json(data, max_bytes=max_bytes)
    if not isinstance(raw, dict):
        raise CleanupCanonicalError("cleanup model must be a JSON object")
    version = raw.get("schema_version")
    if version != "1.0.0":
        raise UnsupportedCleanupVersion("unsupported cleanup schema version")
    try:
        return model.model_validate_json(data, strict=True)
    except ValidationError as exc:
        raise CleanupCanonicalError("cleanup model failed strict validation") from exc


def run_id_sha256(run_id: str) -> str:
    return canonical_cleanup_sha256(RUN_ID_DOMAIN, {"run_id": run_id})


def cleanup_root_marker_sha256(marker: CleanupRootMarkerV1) -> str:
    return canonical_cleanup_sha256(ROOT_MARKER_DOMAIN, marker)


def tree_inventory_sha256(inventory: TreeInventoryV1) -> str:
    return canonical_cleanup_sha256(TREE_INVENTORY_DOMAIN, inventory)


def cleanup_plan_sha256(plan: CleanupPlanV1) -> str:
    return canonical_cleanup_sha256(PLAN_DOMAIN, plan)


def cleanup_approval_binding_sha256(binding: CleanupApprovalBindingV1) -> str:
    return canonical_cleanup_sha256(APPROVAL_BINDING_DOMAIN, binding)


def cleanup_receipt_sha256(receipt: CleanupReceiptV1) -> str:
    return canonical_cleanup_sha256(RECEIPT_DOMAIN, receipt)


def cleanup_tombstone_sha256(tombstone: CleanupTombstoneV1) -> str:
    return canonical_cleanup_sha256(TOMBSTONE_DOMAIN, tombstone)


__all__ = [
    "ACTION_DOMAIN",
    "APPROVAL_BINDING_DOMAIN",
    "MANIFEST_DOMAIN",
    "PLAN_DOMAIN",
    "POINTER_DOMAIN",
    "RECEIPT_DOMAIN",
    "RECONCILIATION_DOMAIN",
    "ROOT_IDENTITY_DOMAIN",
    "ROOT_MARKER_DOMAIN",
    "RUN_ID_DOMAIN",
    "TOMBSTONE_DOMAIN",
    "TRANSACTION_DOMAIN",
    "TREE_IDENTITY_DOMAIN",
    "TREE_INVENTORY_DOMAIN",
    "CleanupCanonicalError",
    "UnsupportedCleanupVersion",
    "canonical_cleanup_bytes",
    "canonical_cleanup_sha256",
    "cleanup_approval_binding_sha256",
    "cleanup_plan_sha256",
    "cleanup_receipt_sha256",
    "cleanup_root_marker_sha256",
    "cleanup_tombstone_sha256",
    "format_cleanup_utc",
    "parse_canonical_cleanup_json",
    "parse_cleanup_model",
    "run_id_sha256",
    "tree_inventory_sha256",
]
