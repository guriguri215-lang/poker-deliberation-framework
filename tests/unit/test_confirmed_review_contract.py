from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.confirmed_review import (
    ConfirmedReviewError,
    admit_confirmed_review,
    candidate_sha256,
    confirmation_sha256,
    create_review_confirmation,
    prepare_review_intake,
)
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
    ConfirmedReviewDiagnosticCode,
    ReviewConfirmationAuthorityV1,
    ReviewIntakePreparationResultV1,
)
from tests.confirmed_review_support import (
    SOURCE_BYTES,
    candidate_payload,
    ready_preparation,
)


def _prepare_source(source: bytes):
    return prepare_review_intake(
        source,
        candidate_payload(),
        source_id="source-unit-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"", ConfirmedReviewDiagnosticCode.SOURCE_SIZE),
        (b"\xef\xbb\xbfsource\n", ConfirmedReviewDiagnosticCode.SOURCE_BOM),
        (b"source\r\n", ConfirmedReviewDiagnosticCode.SOURCE_NEWLINE),
        (b"\xff", ConfirmedReviewDiagnosticCode.SOURCE_UTF8),
        ("e\u0301\n".encode(), ConfirmedReviewDiagnosticCode.SOURCE_NFC),
        (b"source\x00\n", ConfirmedReviewDiagnosticCode.SOURCE_CONTROL),
        (b"api_key=sk-abcdefgh\n", ConfirmedReviewDiagnosticCode.SOURCE_SECRET),
        (
            b"I am currently playing poker right now. What should I do?\n",
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
    ],
)
def test_source_contract_fails_closed_with_stable_codes(
    source: bytes,
    expected: ConfirmedReviewDiagnosticCode,
) -> None:
    result = _prepare_source(source)
    assert result.status == "blocked"
    assert result.candidate is None
    assert [item.code for item in result.diagnostics] == [expected]


def test_source_size_limit_is_exact() -> None:
    accepted = _prepare_source(b"x" * MAX_CONFIRMED_REVIEW_SOURCE_BYTES)
    rejected = _prepare_source(b"x" * (MAX_CONFIRMED_REVIEW_SOURCE_BYTES + 1))
    assert accepted.status == "ready"
    assert rejected.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_SIZE


def test_source_rights_matrix_is_closed() -> None:
    result = prepare_review_intake(
        SOURCE_BYTES,
        candidate_payload(),
        source_id="source-rights-1",
        source_kind="user_supplied",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_RIGHTS


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["hand"].update({"hero_cards": []}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING,
        ),
        (
            lambda value: value["hand"].update({"actions": []}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING,
        ),
        (
            lambda value: value["ambiguities"].append(
                {
                    "ambiguity_id": "ambiguity-1",
                    "field_path": "hand.actions.2.amount",
                    "description": "raise amount is unclear",
                    "status": "unresolved",
                }
            ),
            ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY,
        ),
        (
            lambda value: value["hand"].update({"game_type": "PLO"}),
            ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE,
        ),
    ],
)
def test_candidate_completeness_and_scope_are_fail_closed(mutation, expected) -> None:
    payload = deepcopy(candidate_payload())
    mutation(payload)
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is expected


def test_candidate_and_confirmation_hashes_are_self_replayable() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    assert prepared.source is not None
    assert prepared.candidate.candidate_sha256 == candidate_sha256(prepared.candidate.projection)
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    now = datetime(2026, 7, 29, 1, tzinfo=UTC)
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-confirmation-1",
        confirmation_id="confirmation-unit-1",
        idempotency_key="idempotency-unit-1",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now,
    )
    assert confirmation.confirmation_sha256 == confirmation_sha256(confirmation)


def test_preparation_rejects_a_mismatched_duplicate_source_projection() -> None:
    prepared = ready_preparation()
    assert prepared.source is not None
    payload = prepared.model_dump(mode="json")
    payload["source"]["content_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        ReviewIntakePreparationResultV1.model_validate(payload, strict=True)


def test_confirmation_requires_out_of_band_exact_hashes() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    with pytest.raises(ConfirmedReviewError) as captured:
        create_review_confirmation(
            prepared.candidate,
            run_id="run-unit-confirmation-2",
            confirmation_id="confirmation-unit-2",
            idempotency_key="idempotency-unit-2",
            authority=authority,
            expected_source_sha256="0" * 64,
            expected_candidate_sha256=prepared.candidate.candidate_sha256,
        )
    assert captured.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING


def test_source_mutation_and_expiry_are_rejected_before_run_admission() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    assert prepared.source is not None
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-unit-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    now = datetime(2026, 7, 29, 1, tzinfo=UTC)
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id="run-unit-admission-1",
        confirmation_id="confirmation-unit-3",
        idempotency_key="idempotency-unit-3",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    with pytest.raises(ConfirmedReviewError) as mutated:
        admit_confirmed_review(
            SOURCE_BYTES + b"mutation\n",
            prepared.candidate,
            confirmation,
            admitted_at=now,
        )
    assert mutated.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING
    with pytest.raises(ConfirmedReviewError) as expired:
        admit_confirmed_review(
            SOURCE_BYTES,
            prepared.candidate,
            confirmation,
            admitted_at=now + timedelta(minutes=2),
        )
    assert expired.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED


def test_legacy_range_shape_cannot_enter_candidate_contract() -> None:
    payload = candidate_payload()
    payload["hand"]["known_ranges"] = [
        {
            "player_id": "villain",
            "notation": "AKs",
            "assumed": True,
        }
    ]
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-legacy-range-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA


def test_candidate_secret_is_not_written_to_preparation_artifact() -> None:
    payload = candidate_payload()
    payload["claims"][0]["text"] = "api_key=sk-abcdefgh"
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-secret-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY


def test_candidate_artifact_size_limit_fails_closed() -> None:
    payload = candidate_payload()
    payload["hand"]["opponent_observations"] = ["x" * MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES]
    result = prepare_review_intake(
        SOURCE_BYTES,
        payload,
        source_id="source-candidate-size-1",
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "blocked"
    assert result.candidate is None
    assert result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA
    assert result.diagnostics[0].field_path == "candidate.size_bytes"
