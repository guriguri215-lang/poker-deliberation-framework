"""Adversarial rejection tests for P2-025A."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from poker_deliberation.runtime_conformance import (
    ConformanceRecordV1,
    ProviderConclusionReferenceV1,
    ResultStatus,
    ResultV1,
    canonical_json_bytes,
    compare_records,
    parse_conformance_record,
)
from poker_deliberation.runtime_conformance.canonical import CanonicalConformanceError
from poker_deliberation.schemas import EpistemicLabel
from tests.runtime_conformance_support import NOW, record_pair


def test_nested_sensitive_canary_is_rejected_without_persisting_a_credential() -> None:
    source, _, _, _ = record_pair()
    payload = source.model_dump(mode="json")
    payload["result"]["summary"] = "Bearer " + ("A" * 24)

    with pytest.raises(ValidationError, match="secret shape"):
        ConformanceRecordV1.model_validate(payload)


def test_duplicate_keys_unknown_fields_and_version_changes_are_rejected() -> None:
    source, _, _, _ = record_pair()
    encoded = canonical_json_bytes(source)
    duplicate = encoded.replace(
        b'"canonicalization":',
        b'"canonicalization":"poker-runtime-conformance-json-v1","canonicalization":',
        1,
    )
    with pytest.raises(CanonicalConformanceError, match="duplicate"):
        parse_conformance_record(duplicate)

    raw = json.loads(encoded)
    raw["unexpected"] = "field"
    with pytest.raises(CanonicalConformanceError, match="strict schema"):
        parse_conformance_record(canonical_json_bytes(raw))

    raw.pop("unexpected")
    raw["schema_version"] = "9.0.0"
    with pytest.raises(CanonicalConformanceError, match="unsupported"):
        parse_conformance_record(canonical_json_bytes(raw))


def test_runtime_bridge_flag_cannot_be_forged() -> None:
    source, _, _, _ = record_pair()
    raw = source.model_dump(mode="json")
    raw["runtime_bridge_used"] = True

    with pytest.raises(ValidationError):
        ConformanceRecordV1.model_validate(raw)


def test_unverified_provider_prose_cannot_promote_a_calculated_claim() -> None:
    with pytest.raises(ValidationError, match="successful tool"):
        ResultV1(
            result_id="provider-only-result",
            status=ResultStatus.SUCCEEDED,
            summary="A provider-only result remains unverified.",
            epistemic_label=EpistemicLabel.CALCULATED,
            provider_conclusions=(
                ProviderConclusionReferenceV1(
                    execution_id="provider-execution",
                    provider_id="local",
                ),
            ),
        )


def test_context_provenance_tampering_is_detected_even_when_payload_hash_is_kept() -> None:
    source, target, codex, python = record_pair()
    provenance = target.assignment.context.provenance.model_copy(update={"source_sha256": "f" * 64})
    context = target.assignment.context.model_copy(update={"provenance": provenance})
    changed = target.model_copy(
        update={"assignment": target.assignment.model_copy(update={"context": context})}
    )

    codes = {
        item.code
        for item in compare_records(
            source,
            changed,
            codex,
            python,
            now=NOW,
        ).violations
    }
    assert "context-provenance-mismatch" in codes
    assert "execution-audit-hash-mismatch" in codes
