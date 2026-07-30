"""Fail-closed preparation, confirmation, admission, and provenance helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from poker_deliberation.agents import select_roles
from poker_deliberation.budgets import BudgetFailureCode
from poker_deliberation.confirmed_review_models import (
    CANDIDATE_CANONICALIZATION_ID,
    CONFIRMATION_CANONICALIZATION_ID,
    CONFIRMED_REVIEW_EXTRACTOR_ID,
    CONFIRMED_REVIEW_EXTRACTOR_VERSION,
    CONFIRMED_REVIEW_TOOL_ALLOWLIST,
    MAX_CONFIRMED_REVIEW_ACTIONS,
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    MAX_CONFIRMED_REVIEW_RANGES,
    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
    PROVENANCE_CANONICALIZATION_ID,
    SOURCE_CANONICALIZATION_ID,
    ConfirmedReviewAgentSupportV1,
    ConfirmedReviewDiagnosticCode,
    ConfirmedReviewDiagnosticV1,
    ConfirmedReviewProvenanceV1,
    ConfirmedReviewToolSupportV1,
    ReviewCandidateInputV1,
    ReviewCandidateProjectionV1,
    ReviewConfirmationAuthorityV1,
    ReviewIntakeCandidateV1,
    ReviewIntakeConfirmationV1,
    ReviewIntakePreparationResultV1,
    ReviewSourceProvenanceV1,
)
from poker_deliberation.context_lifecycle import (
    ContextLifecycleError,
    build_context_envelope,
    context_payload,
    legacy_context_sha256,
)
from poker_deliberation.phases.services import (
    build_agent_context,
    is_verified_claim_correction,
)
from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentReport,
    CanonicalHand,
    CaseInput,
    Claim,
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.security import (
    real_time_assistance_signals,
    redact_sensitive,
    screen_case,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
)
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.hand_pot_ledger import (
    PROFILE_ID,
    PROFILE_SCHEMA_VERSION,
    PROFILE_VERSION,
    SUPPORTED_SITE,
)


class ConfirmedReviewError(ValueError):
    """Stable fail-closed error that never includes caller-provided text."""

    def __init__(
        self,
        code: ConfirmedReviewDiagnosticCode,
        field_path: str,
    ) -> None:
        super().__init__(code.value)
        self.code = code
        self.field_path = field_path


def _fail(code: ConfirmedReviewDiagnosticCode, field_path: str) -> NoReturn:
    raise ConfirmedReviewError(code, field_path)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _domain_sha256(domain: str, value: Any) -> str:
    return _sha256(domain.encode("ascii") + b"\x00" + canonical_json_bytes(value))


def _without_hash(model: BaseModel, hash_field: str) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    del payload[hash_field]
    return payload


def candidate_sha256(projection: ReviewCandidateProjectionV1) -> str:
    return _domain_sha256(
        CANDIDATE_CANONICALIZATION_ID,
        projection.model_dump(mode="json"),
    )


def authority_snapshot_sha256(authority: ReviewConfirmationAuthorityV1) -> str:
    return _domain_sha256(
        CONFIRMATION_CANONICALIZATION_ID + ":authority",
        authority.model_dump(mode="json"),
    )


def confirmation_sha256(confirmation: ReviewIntakeConfirmationV1) -> str:
    return _domain_sha256(
        CONFIRMATION_CANONICALIZATION_ID,
        _without_hash(confirmation, "confirmation_sha256"),
    )


def provenance_sha256(provenance: ConfirmedReviewProvenanceV1) -> str:
    return _domain_sha256(
        PROVENANCE_CANONICALIZATION_ID,
        _without_hash(provenance, "provenance_sha256"),
    )


def _strict_candidate(candidate: ReviewIntakeCandidateV1) -> ReviewIntakeCandidateV1:
    try:
        payload = canonical_json_bytes(candidate)
    except (CanonicalStorageError, TypeError, ValueError):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA, "candidate")
    if len(payload) > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA, "candidate.size_bytes")
    try:
        return ReviewIntakeCandidateV1.model_validate_json(payload, strict=True)
    except ValidationError:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA, "candidate")


def _strict_authority(
    authority: ReviewConfirmationAuthorityV1,
) -> ReviewConfirmationAuthorityV1:
    try:
        payload = canonical_json_bytes(authority)
        return ReviewConfirmationAuthorityV1.model_validate_json(payload, strict=True)
    except (CanonicalStorageError, TypeError, ValueError, ValidationError):
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_AUTHORITY, "confirmation.authority")


def _strict_confirmation(
    confirmation: ReviewIntakeConfirmationV1,
) -> ReviewIntakeConfirmationV1:
    try:
        payload = canonical_json_bytes(confirmation)
    except (CanonicalStorageError, TypeError, ValueError):
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    if len(payload) > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "confirmation.size_bytes")
    try:
        return ReviewIntakeConfirmationV1.model_validate_json(payload, strict=True)
    except ValidationError as exc:
        if any(error["loc"] and error["loc"][0] == "authority" for error in exc.errors()):
            _fail(
                ConfirmedReviewDiagnosticCode.CONFIRMATION_AUTHORITY,
                "confirmation.authority",
            )
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "confirmation")


def _strict_provenance(
    provenance: ConfirmedReviewProvenanceV1,
) -> ConfirmedReviewProvenanceV1:
    try:
        payload = canonical_json_bytes(provenance)
    except (CanonicalStorageError, TypeError, ValueError):
        _fail(ConfirmedReviewDiagnosticCode.STORAGE, "confirmed_review_provenance.json")
    if len(payload) > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES:
        _fail(ConfirmedReviewDiagnosticCode.STORAGE, "confirmed_review_provenance.json")
    try:
        return ConfirmedReviewProvenanceV1.model_validate_json(payload, strict=True)
    except ValidationError:
        _fail(ConfirmedReviewDiagnosticCode.STORAGE, "confirmed_review_provenance.json")


def _diagnostic(
    code: ConfirmedReviewDiagnosticCode,
    field_path: str,
) -> ConfirmedReviewDiagnosticV1:
    return ConfirmedReviewDiagnosticV1(code=code, field_path=field_path)


def _source_provenance(
    source_bytes: bytes,
    *,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal[
        "local_analysis_only",
        "redistribution_allowed",
    ],
    classification: Literal["internal", "public"],
) -> ReviewSourceProvenanceV1:
    try:
        return ReviewSourceProvenanceV1(
            source_id=source_id,
            source_kind=source_kind,
            license_classification=license_classification,
            usage_classification=usage_classification,
            classification=classification,
            bytes_length=len(source_bytes),
            content_sha256=_sha256(
                SOURCE_CANONICALIZATION_ID.encode("ascii") + b"\x00" + source_bytes
            ),
        )
    except ValidationError as exc:
        code = ConfirmedReviewDiagnosticCode.SOURCE_RIGHTS
        if any(error["loc"] == ("classification",) for error in exc.errors()):
            code = ConfirmedReviewDiagnosticCode.SOURCE_CLASSIFICATION
        raise ConfirmedReviewError(code, "source.provenance") from None


def validate_review_source(
    source_bytes: bytes,
    *,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal[
        "local_analysis_only",
        "redistribution_allowed",
    ],
    classification: Literal["internal", "public"],
) -> ReviewSourceProvenanceV1:
    """Validate the exact source bytes before any durable namespace exists."""

    if not source_bytes or len(source_bytes) > MAX_CONFIRMED_REVIEW_SOURCE_BYTES:
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_SIZE, "source")
    if source_bytes.startswith(b"\xef\xbb\xbf"):
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_BOM, "source")
    if b"\r" in source_bytes:
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_NEWLINE, "source")
    try:
        source_text = source_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_UTF8, "source")
    if unicodedata.normalize("NFC", source_text) != source_text:
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_NFC, "source")
    for character in source_text:
        category = unicodedata.category(character)
        if character not in {"\n", "\t"} and (
            category == "Cf" or category == "Cc" or 0x7F <= ord(character) <= 0x9F
        ):
            _fail(ConfirmedReviewDiagnosticCode.SOURCE_CONTROL, "source")
    if redact_sensitive(source_text) != source_text:
        _fail(ConfirmedReviewDiagnosticCode.SOURCE_SECRET, "source")
    source_probe = CaseInput(
        kind="strategy",
        raw_text=source_text,
        analysis_scope="retrospective",
    )
    if any(event.blocked for event in screen_case(source_probe)):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "source")
    return _source_provenance(
        source_bytes,
        source_id=source_id,
        source_kind=source_kind,
        license_classification=license_classification,
        usage_classification=usage_classification,
        classification=classification,
    )


def _candidate_field_path(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "candidate"
    location = ".".join(str(part) for part in errors[0]["loc"])
    return f"candidate.{location}" if location else "candidate"


def _validate_candidate_scope(candidate: ReviewCandidateInputV1) -> None:
    hand = candidate.hand
    candidate_value = candidate.model_dump(mode="json")
    if redact_sensitive(candidate_value) != candidate_value:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY, "candidate")
    if hand.game_type != "NLHE" or hand.analysis_objective != "strategy_review":
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    if hand.hero_player_id is None or len(hand.hero_cards) != 2:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING, "candidate.hand.hero")
    if len(hand.players) != hand.table_size or not hand.actions:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING, "candidate.hand")
    if len(hand.actions) > MAX_CONFIRMED_REVIEW_ACTIONS:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand.actions")
    if any(item.status == "unresolved" for item in candidate.ambiguities):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY, "candidate.ambiguities")
    if len(hand.known_ranges) > MAX_CONFIRMED_REVIEW_RANGES:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_COUNT, "candidate.hand.known_ranges")
    if any(not isinstance(item, VersionedRangeDefinitionV1) for item in hand.known_ranges):
        _fail(
            ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED,
            "candidate.hand.known_ranges",
        )
    profile = candidate.ledger_profile
    if profile is not None and (
        profile.schema_version != PROFILE_SCHEMA_VERSION
        or profile.profile_id != PROFILE_ID
        or profile.profile_version != PROFILE_VERSION
        or profile.supported_site != SUPPORTED_SITE
        or hand.format != "cash"
        or hand.rake != 0
        or hand.ante != 0
    ):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL, "candidate.ledger_profile")


def prepare_review_intake(
    source_bytes: bytes,
    candidate_payload: object,
    *,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal[
        "local_analysis_only",
        "redistribution_allowed",
    ],
    classification: Literal["internal", "public"],
) -> ReviewIntakePreparationResultV1:
    """Create a deterministic candidate envelope without admitting a run."""

    try:
        source = validate_review_source(
            source_bytes,
            source_id=source_id,
            source_kind=source_kind,
            license_classification=license_classification,
            usage_classification=usage_classification,
            classification=classification,
        )
    except ConfirmedReviewError as exc:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            diagnostics=(_diagnostic(exc.code, exc.field_path),),
        )
    try:
        candidate_bytes = canonical_json_bytes(candidate_payload)
        if len(candidate_bytes) > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES:
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA,
                "candidate.size_bytes",
            )
        candidate_input = ReviewCandidateInputV1.model_validate_json(
            candidate_bytes,
            strict=True,
        )
        # Pydantic does not revalidate omitted scalar defaults by default
        # (for example CanonicalHand.ante starts as integer 0 although its
        # contract is float).  Round-trip the nested hand once so candidate
        # canonical bytes are stable under every durable reader.
        canonical_hand = CanonicalHand.model_validate_json(
            canonical_json_bytes(candidate_input.hand),
            strict=True,
        )
        candidate_input = candidate_input.model_copy(
            update={"hand": canonical_hand},
        )
        _validate_candidate_scope(candidate_input)
    except ValidationError as exc:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            source=source,
            diagnostics=(
                _diagnostic(
                    ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA,
                    _candidate_field_path(exc),
                ),
            ),
        )
    except CanonicalStorageError:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            source=source,
            diagnostics=(
                _diagnostic(
                    ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA,
                    "candidate",
                ),
            ),
        )
    except ConfirmedReviewError as exc:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            source=source,
            diagnostics=(_diagnostic(exc.code, exc.field_path),),
        )
    projection = ReviewCandidateProjectionV1(
        source=source,
        candidate_input=candidate_input,
    )
    candidate = ReviewIntakeCandidateV1(
        projection=projection,
        candidate_sha256=candidate_sha256(projection),
    )
    try:
        _validate_combined_security_scope(source_bytes, candidate)
    except ConfirmedReviewError as exc:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            source=source,
            diagnostics=(_diagnostic(exc.code, exc.field_path),),
        )
    if len(canonical_json_bytes(candidate)) > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES:
        return ReviewIntakePreparationResultV1(
            status="blocked",
            source=source,
            diagnostics=(
                _diagnostic(
                    ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA,
                    "candidate.size_bytes",
                ),
            ),
        )
    return ReviewIntakePreparationResultV1(
        status="ready",
        source=source,
        candidate=candidate,
    )


def verify_candidate(candidate: ReviewIntakeCandidateV1) -> ReviewIntakeCandidateV1:
    candidate = _strict_candidate(candidate)
    if candidate.candidate_sha256 != candidate_sha256(candidate.projection):
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "candidate.candidate_sha256")
    _validate_candidate_scope(candidate.projection.candidate_input)
    return candidate


def create_review_confirmation(
    candidate: ReviewIntakeCandidateV1,
    *,
    run_id: str,
    confirmation_id: str,
    idempotency_key: str,
    authority: ReviewConfirmationAuthorityV1,
    expected_source_sha256: str,
    expected_candidate_sha256: str,
    confirmed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ReviewIntakeConfirmationV1:
    """Bind an explicit confirmation to hashes supplied out of band by the user."""

    candidate = verify_candidate(candidate)
    authority = _strict_authority(authority)
    if (
        expected_source_sha256 != candidate.projection.source.content_sha256
        or expected_candidate_sha256 != candidate.candidate_sha256
    ):
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "confirmation.expected_hashes")
    confirmed_time = confirmed_at or datetime.now(UTC)
    expiry = expires_at or confirmed_time + timedelta(hours=24)
    provisional = ReviewIntakeConfirmationV1(
        run_id=run_id,
        intake_id=candidate.projection.candidate_input.intake_id,
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        source_sha256=expected_source_sha256,
        candidate_sha256=expected_candidate_sha256,
        authority=authority,
        authority_snapshot_sha256=authority_snapshot_sha256(authority),
        confirmed_at=confirmed_time,
        expires_at=expiry,
        confirmation_sha256="0" * 64,
    )
    return provisional.model_copy(update={"confirmation_sha256": confirmation_sha256(provisional)})


@dataclass(frozen=True, slots=True)
class ConfirmedReviewAdmission:
    source_bytes: bytes
    candidate: ReviewIntakeCandidateV1
    confirmation: ReviewIntakeConfirmationV1
    admitted_at: datetime
    case: CaseInput


def _case_from_candidate(candidate: ReviewIntakeCandidateV1) -> CaseInput:
    candidate_input = candidate.projection.candidate_input
    requested_tools = ["hand_validator"]
    metadata: dict[str, Any] = {
        "confirmed_review": {
            "contract_id": candidate.contract_id,
            "intake_id": candidate_input.intake_id,
        }
    }
    if candidate_input.ledger_profile is not None:
        requested_tools.append("hand_pot_ledger")
        metadata["tool_inputs"] = {
            "hand_pot_ledger": {
                "schema_version": "1.0.0",
                "rule_profile": candidate_input.ledger_profile.model_dump(mode="json"),
            }
        }
    if candidate_input.hand.known_ranges:
        requested_tools.append("combos")
    claims = [
        Claim(
            claim_id=item.claim_id,
            text=item.text,
            label=EpistemicLabel.USER_CLAIM,
            confidence=ConfidenceGrade(item.confidence),
        )
        for item in candidate_input.claims
    ]
    return CaseInput(
        case_id=f"confirmed-review-{candidate_input.intake_id}",
        kind="hand",
        hand=candidate_input.hand,
        analysis_scope="retrospective",
        claims=claims,
        objective="confirmed_natural_language_review",
        requested_tools=requested_tools,
        metadata=metadata,
    )


def _validate_combined_security_scope(
    source_bytes: bytes,
    candidate: ReviewIntakeCandidateV1,
) -> CaseInput:
    case = _case_from_candidate(candidate)
    if screen_case(case):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    source_text = source_bytes.decode("utf-8", errors="strict")
    candidate_claims = [claim.text for claim in case.claims]
    source_boundary = source_text[-512:]
    if any(
        redact_sensitive(source_boundary + claim[:512]) != source_boundary + claim[:512]
        for claim in candidate_claims
    ):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SECURITY, "candidate")
    candidate_live, candidate_decision, candidate_explicit = real_time_assistance_signals(
        candidate_claims
    )
    if candidate_explicit:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    combined_live, combined_decision, combined_explicit = real_time_assistance_signals(
        [source_text, *candidate_claims]
    )
    if combined_explicit or (combined_live and combined_decision):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    joined_live, joined_decision, joined_explicit = real_time_assistance_signals(
        "".join((source_text, *candidate_claims))
    )
    if joined_explicit or (joined_live and joined_decision):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    if candidate_live and candidate_decision:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_SCOPE, "candidate.hand")
    return case


def _admit_confirmed_review_at(
    source_bytes: bytes,
    candidate: ReviewIntakeCandidateV1,
    confirmation: ReviewIntakeConfirmationV1,
    *,
    admitted_at: datetime,
) -> ConfirmedReviewAdmission:
    """Validate one admission at a trusted execution or historical replay time."""

    candidate = verify_candidate(candidate)
    confirmation = _strict_confirmation(confirmation)
    if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "admission.admitted_at")
    source = validate_review_source(
        source_bytes,
        source_id=candidate.projection.source.source_id,
        source_kind=candidate.projection.source.source_kind,
        license_classification=candidate.projection.source.license_classification,
        usage_classification=candidate.projection.source.usage_classification,
        classification=candidate.projection.source.classification,
    )
    if source != candidate.projection.source:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "source")
    if confirmation.confirmation_sha256 != confirmation_sha256(confirmation):
        _fail(
            ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING,
            "confirmation.confirmation_sha256",
        )
    if confirmation.authority_snapshot_sha256 != authority_snapshot_sha256(confirmation.authority):
        _fail(
            ConfirmedReviewDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority_snapshot_sha256",
        )
    candidate_input = candidate.projection.candidate_input
    if (
        confirmation.intake_id != candidate_input.intake_id
        or confirmation.source_sha256 != source.content_sha256
        or confirmation.candidate_sha256 != candidate.candidate_sha256
        or confirmation.extractor_id != CONFIRMED_REVIEW_EXTRACTOR_ID
        or confirmation.extractor_version != CONFIRMED_REVIEW_EXTRACTOR_VERSION
    ):
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    if confirmation.confirmed_at > admitted_at or admitted_at > confirmation.expires_at:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED, "confirmation.expires_at")
    for range_definition in candidate_input.hand.known_ranges:
        if not isinstance(range_definition, VersionedRangeDefinitionV1):
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED,
                "candidate.hand.known_ranges",
            )
        validation = validate_versioned_range(candidate_input.hand, range_definition)
        if validation.status != "success":
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED,
                "candidate.hand.known_ranges",
            )
    case = _validate_combined_security_scope(source_bytes, candidate)
    if not set(case.requested_tools).issubset(CONFIRMED_REVIEW_TOOL_ALLOWLIST):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL, "candidate.requested_tools")
    return ConfirmedReviewAdmission(
        source_bytes=source_bytes,
        candidate=candidate,
        confirmation=confirmation,
        admitted_at=admitted_at,
        case=case,
    )


def admit_confirmed_review(
    source_bytes: bytes,
    candidate: ReviewIntakeCandidateV1,
    confirmation: ReviewIntakeConfirmationV1,
) -> ConfirmedReviewAdmission:
    """Admit a new run only against this process's current trusted UTC clock."""

    return _admit_confirmed_review_at(
        source_bytes,
        candidate,
        confirmation,
        admitted_at=datetime.now(UTC),
    )


def _tool_label(result: ToolResult) -> EpistemicLabel:
    if result.status is not ToolStatus.SUCCESS:
        return EpistemicLabel.UNKNOWN
    if result.numeric_exactness in {
        NumericalExactness.EXACT,
        NumericalExactness.EXACT_UNDER_MODEL,
        NumericalExactness.FLOATING_VERIFIED,
    }:
        return EpistemicLabel.CALCULATED
    if result.numeric_exactness is NumericalExactness.APPROXIMATE:
        return EpistemicLabel.ESTIMATE
    return EpistemicLabel.UNKNOWN


def _tool_support(result: ToolResult) -> ConfirmedReviewToolSupportV1:
    if result.tool_name not in CONFIRMED_REVIEW_TOOL_ALLOWLIST:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL, "report.tool_results")
    label = _tool_label(result)
    if (
        result.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL
        and not result.model_qualifier
    ):
        _fail(ConfirmedReviewDiagnosticCode.REPORT_OVERREACH, "report.tool_results")
    if result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED and (
        result.verification is None or not result.verification.passed
    ):
        _fail(ConfirmedReviewDiagnosticCode.REPORT_OVERREACH, "report.tool_results")
    return ConfirmedReviewToolSupportV1(
        result_id=result.result_id,
        tool_name=cast(
            Literal[
                "hand_validator",
                "hand_pot_ledger",
                "range_validate",
                "combos",
            ],
            result.tool_name,
        ),
        tool_version=result.version,
        contract_version=result.contract_version,
        status=result.status.value,
        epistemic_label=cast(
            Literal["CALCULATED", "ESTIMATE", "UNKNOWN"],
            label.value,
        ),
        input_sha256=_domain_sha256(
            PROVENANCE_CANONICALIZATION_ID + ":tool-input",
            result.input,
        ),
        output_sha256=_domain_sha256(
            PROVENANCE_CANONICALIZATION_ID + ":tool-output",
            result.output,
        ),
        result_sha256=_domain_sha256(
            PROVENANCE_CANONICALIZATION_ID + ":tool-result",
            result.model_dump(mode="json"),
        ),
    )


def _expected_tool_results(
    admission: ConfirmedReviewAdmission,
) -> dict[str, ToolResult]:
    hand = admission.case.hand
    if hand is None:
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING, "candidate.hand")
    hand_payload = hand.model_dump(mode="json")
    expected_inputs: dict[str, dict[str, Any]] = {"hand_validator": hand_payload}
    candidate_input = admission.candidate.projection.candidate_input
    if candidate_input.ledger_profile is not None:
        raw_tool_inputs = admission.case.metadata.get("tool_inputs")
        ledger_payload = (
            raw_tool_inputs.get("hand_pot_ledger") if isinstance(raw_tool_inputs, dict) else None
        )
        if not isinstance(ledger_payload, dict):
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL,
                "candidate.ledger_profile",
            )
        exact_ledger_payload = {**ledger_payload, "hand": hand_payload}
        expected_inputs["hand_pot_ledger"] = exact_ledger_payload
    if candidate_input.hand.known_ranges:
        range_definition = candidate_input.hand.known_ranges[0]
        if not isinstance(range_definition, VersionedRangeDefinitionV1):
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED,
                "candidate.hand.known_ranges",
            )
        validation = validate_versioned_range(candidate_input.hand, range_definition)
        if validation.status != "success" or validation.canonical_notation is None:
            _fail(
                ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED,
                "candidate.hand.known_ranges",
            )
        canonical_notation = validation.canonical_notation
        range_payload = {
            "schema_version": "1.0.0",
            "hand": hand_payload,
            "range_definition": range_definition.model_dump(mode="json"),
        }
        expected_inputs["range_validate"] = range_payload
        expected_inputs["combos"] = {
            "range": canonical_notation,
            "dead_cards": [],
        }
    registry = default_registry()
    expected: dict[str, ToolResult] = {}
    for tool_name, payload in expected_inputs.items():
        result = registry.execute(tool_name, payload)
        if result.status is not ToolStatus.SUCCESS:
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.tool_results",
            )
        expected[tool_name] = result
    return expected


def _tool_result_semantic_projection(result: ToolResult) -> dict[str, Any]:
    return result.model_dump(
        mode="json",
        exclude={"result_id", "duration_seconds", "created_at"},
    )


_CONFIRMED_REVIEW_TOOL_MAX_DURATION_SECONDS = 30.0
_CONFIRMED_REVIEW_CONTEXT_MAX_DURATION_SECONDS = 30.0
_CONFIRMED_REVIEW_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{12}")
_CONFIRMED_REVIEW_CONTEXT_ID = re.compile(r"context-[0-9a-f]{24}")
_CONFIRMED_REVIEW_CONTEXT_ATTEMPT_ID = re.compile(r"attempt-[0-9a-f]{24}")
_CONFIRMED_REVIEW_EXECUTION_ID = re.compile(r"execution-[0-9a-f]{24}")
_CONFIRMED_UNVERIFIED_CLAIM_WARNING = (
    "ユーザー主張は入力として保存しましたが、検証条件がないため真偽未判定です。"
)
_CONFIRMED_SOLVER_LIMITATION = "外部ソルバーの実行・収束確認なしにGTOまたは均衡を主張していません。"
_CONFIRMED_RUNTIME_DATA_QUALITY = frozenset(
    {
        "strict runtime refused before hand validation",
        "provider analysis skipped because round budget is zero",
        "maximum runtime reached before provider analysis",
        "maximum runtime reached during context build",
        "maximum runtime reached during provider preflight",
        "maximum runtime exceeded after provider analysis",
        "strict runtime refused before versioned range validation",
        "strict runtime refused before requested tool execution",
        "maximum runtime exceeded after tool execution",
        "confirmed terminal publication refused with less than 0.25 seconds remaining",
        "maximum runtime exceeded during final synthesis",
        "maximum runtime exceeded during final artifact writes",
    }
)


def _validate_reproduction_steps(report: FinalReport) -> None:
    expected_results = [
        result for result in report.tool_results if result.reproduce_command is not None
    ]
    if len(report.reproduction_steps) != len(expected_results):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.reproduction_steps",
        )
    for step, result in zip(report.reproduction_steps, expected_results, strict=True):
        prefix = "argv-json: "
        if not step.startswith(prefix):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.reproduction_steps",
            )
        try:
            argv = json.loads(step.removeprefix(prefix))
        except (json.JSONDecodeError, TypeError):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.reproduction_steps",
            )
        if (
            not isinstance(argv, list)
            or len(argv) != 7
            or argv[:6]
            != [
                "poker-deliberate",
                "calculate",
                result.tool_name,
                "--analysis-scope",
                "retrospective",
                "--input",
            ]
            or not isinstance(argv[6], str)
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.reproduction_steps",
            )
        normalized_path = argv[6].replace("\\", "/")
        path_parts = tuple(part for part in normalized_path.split("/") if part)
        if (
            re.match(r"^(?:[A-Za-z]:/|/)", normalized_path) is None
            or any(part in {".", ".."} for part in path_parts)
            or len(path_parts) < 8
            or path_parts[-8:-4] != ("runs", report.run_id, ".terminal-store", "revisions")
            or re.fullmatch(r"r1-txn-[0-9a-f]{32}", path_parts[-4]) is None
            or path_parts[-3:] != ("payload", "tool_results", f"{result.result_id}.input.json")
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.reproduction_steps",
            )


def _validate_confirmed_report_projection(report: FinalReport) -> None:
    if (
        report.alternatives
        or report.sensitivity
        or report.disputes
        or report.evidence
        or report.approvals
        or report.security_events
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.authoritative_fields",
        )
    if report.confidence is not ConfidenceGrade.C:
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.confidence",
        )
    unique_data_quality = list(dict.fromkeys(report.data_quality))
    unique_limitations = list(dict.fromkeys(report.limitations))
    tool_messages = {
        str(message)
        for result in report.tool_results
        for message in (
            *result.warnings,
            *(
                result.output.get("warnings", [])
                if isinstance(result.output.get("warnings", []), list)
                else []
            ),
            *(
                result.output.get("errors", [])
                if isinstance(result.output.get("errors", []), list)
                else []
            ),
        )
    }
    role_names = {record.agent_role for record in report.agent_execution_records}
    budget_codes = {code.value for code in BudgetFailureCode}

    def runtime_data_quality(item: str) -> bool:
        if item in _CONFIRMED_RUNTIME_DATA_QUALITY:
            return True
        for prefix in ("strict usage settlement failed: ", "strict budget failure: "):
            if item.startswith(prefix) and item.removeprefix(prefix) in budget_codes:
                return True
        return any(
            item == f"provider {role} context expired"
            or item == f"provider {role} context rejected: context envelope has expired"
            or item == f"provider {role} output exceeded the hard byte limit"
            or (
                item.startswith(f"provider {role} budget refused: ")
                and item.removeprefix(f"provider {role} budget refused: ") in budget_codes
            )
            for role in role_names
        )

    def allowed_data_quality(item: str) -> bool:
        return (
            item in tool_messages
            or item == _CONFIRMED_UNVERIFIED_CLAIM_WARNING
            or runtime_data_quality(item)
        )

    expected_limitations = list(dict.fromkeys([*report.data_quality, _CONFIRMED_SOLVER_LIMITATION]))
    if (
        report.data_quality != unique_data_quality
        or report.limitations != unique_limitations
        or not all(allowed_data_quality(item) for item in report.data_quality)
        or report.limitations != expected_limitations
        or (
            report.run_status == "completed"
            and any(runtime_data_quality(item) for item in report.data_quality)
        )
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.limitations",
        )
    _validate_reproduction_steps(report)


def _expected_agent_context_fields(
    *,
    admission: ConfirmedReviewAdmission,
    report: FinalReport,
    record: AgentExecutionRecord,
    assignment: AgentAssignment,
    assignment_template: AgentAssignment,
    assignment_is_authoritative: bool,
    registered_tools: frozenset[str],
) -> dict[str, Any]:
    try:
        context = build_agent_context(
            admission.case,
            record.agent_role,
            registered_tools,
        )
        expected_assignment = AgentAssignment.model_validate(
            assignment_template.model_copy(
                update={
                    "assignment_id": assignment.assignment_id,
                    "context_keys": sorted(context_payload(context)),
                },
                deep=True,
            ).model_dump(mode="python")
        )
        if assignment_is_authoritative and assignment != expected_assignment:
            raise ValueError("assignment ledger does not match the canonical context")
        assignment = expected_assignment
        envelope = build_context_envelope(
            context,
            assignment,
            run_id=report.run_id,
            expires_at=cast(datetime, record.context_expires_at),
            clock=lambda: record.started_at,
            context_id=record.context_id,
            attempt_id=record.context_attempt_id,
        )
    except (ContextLifecycleError, TypeError, ValueError):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.agent_execution_records",
        )
    return {
        "assignment_id": assignment.assignment_id,
        "agent_role": assignment.agent_role,
        "context_sha256": legacy_context_sha256(context),
        "context_id": envelope.lineage.context_id,
        "context_attempt_id": envelope.lineage.attempt_id,
        "parent_context_id": envelope.lineage.parent_context_id,
        "context_schema_version": envelope.schema_version,
        "context_classification": envelope.policy.classification.value,
        "context_payload_sha256": envelope.payload_sha256,
        "context_source_sha256": envelope.lineage.source_sha256,
        "context_policy_sha256": envelope.policy_sha256,
        "context_envelope_sha256": envelope.integrity_sha256,
        "context_expires_at": envelope.policy.expires_at,
        "context_producer_runtime": envelope.lineage.producer_runtime.value,
        "context_consumer_runtime": envelope.lineage.consumer_runtime.value,
    }


def build_confirmed_review_provenance(
    admission: ConfirmedReviewAdmission,
    report: FinalReport,
    *,
    assignments: Sequence[AgentAssignment] | None = None,
    agent_reports: Sequence[AgentReport] | None = None,
) -> ConfirmedReviewProvenanceV1:
    """Build the typed authority wrapper after the ordinary report is complete."""

    verified_admission = _admit_confirmed_review_at(
        admission.source_bytes,
        admission.candidate,
        admission.confirmation,
        admitted_at=admission.admitted_at,
    )
    if verified_admission != admission:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "admission")
    admission = verified_admission
    if report.run_id != admission.confirmation.run_id:
        _fail(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING, "report.run_id")
    if report.reconstructed_input != admission.case.model_dump(mode="json"):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.reconstructed_input",
        )
    expected_marker = admission.case.metadata.get("confirmed_review")
    report_metadata = report.reconstructed_input.get("metadata")
    report_marker_present = isinstance(report_metadata, dict) and (
        "confirmed_review" in report_metadata
    )
    report_marker = (
        report_metadata.get("confirmed_review") if isinstance(report_metadata, dict) else None
    )
    if (
        "confirmed_review" not in admission.case.metadata
        or not report_marker_present
        or report_marker != expected_marker
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.reconstructed_input.metadata.confirmed_review",
        )
    if report.claim_assessments != admission.case.claims:
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.claim_assessments",
        )
    if (
        report.generated_at.tzinfo is None
        or report.generated_at.utcoffset() is None
        or report.generated_at < admission.admitted_at
        or report.generated_at > admission.confirmation.expires_at
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.generated_at",
        )
    expected_tool_results = _expected_tool_results(admission)
    expected_tool_names = list(expected_tool_results)
    actual_tool_names = [result.tool_name for result in report.tool_results]
    if (report.run_status == "completed" and actual_tool_names != expected_tool_names) or (
        report.run_status == "failed_with_limitations"
        and actual_tool_names != expected_tool_names[: len(actual_tool_names)]
    ):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL, "report.tool_results")
    _validate_confirmed_report_projection(report)
    result_ids = [result.result_id for result in report.tool_results]
    if len(set(result_ids)) != len(result_ids):
        _fail(ConfirmedReviewDiagnosticCode.REPORT_OVERREACH, "report.tool_results")
    tool_support = tuple(_tool_support(result) for result in report.tool_results)
    validator_results = [
        result for result in report.tool_results if result.tool_name == "hand_validator"
    ]
    validator_required = report.run_status == "completed" or bool(report.tool_results)
    if validator_required and (
        len(validator_results) != 1
        or validator_results[0].status is not ToolStatus.SUCCESS
        or validator_results[0].output.get("valid") is not True
    ):
        _fail(ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING, "report.hand_validator")
    for result in report.tool_results:
        if (
            result.status is not ToolStatus.SUCCESS
            or result.duration_seconds > _CONFIRMED_REVIEW_TOOL_MAX_DURATION_SECONDS
            or result.created_at.tzinfo is None
            or result.created_at.utcoffset() is None
            or result.created_at < admission.admitted_at
            or result.created_at > report.generated_at
            or _tool_result_semantic_projection(result)
            != _tool_result_semantic_projection(expected_tool_results[result.tool_name])
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.tool_results",
            )
    agents: list[ConfirmedReviewAgentSupportV1] = []
    execution_ids = [record.execution_id for record in report.agent_execution_records]
    assignment_ids = [record.assignment_id for record in report.agent_execution_records]
    context_ids = [record.context_id for record in report.agent_execution_records]
    context_attempt_ids = [record.context_attempt_id for record in report.agent_execution_records]
    if (
        len(set(execution_ids)) != len(execution_ids)
        or len(set(assignment_ids)) != len(assignment_ids)
        or None in context_ids
        or len(set(context_ids)) != len(context_ids)
        or None in context_attempt_ids
        or len(set(context_attempt_ids)) != len(context_attempt_ids)
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.agent_execution_records",
        )
    assignment_template_sequence = tuple(select_roles(admission.case))
    assignment_templates = {
        assignment.agent_role: assignment for assignment in assignment_template_sequence
    }
    expected_roles = [assignment.agent_role for assignment in assignment_template_sequence]
    actual_roles = [record.agent_role for record in report.agent_execution_records]
    if report.run_status not in {"completed", "failed_with_limitations"}:
        _fail(ConfirmedReviewDiagnosticCode.REPORT_OVERREACH, "report.run_status")
    if (report.run_status == "completed" and actual_roles != expected_roles) or (
        report.run_status == "failed_with_limitations"
        and actual_roles != expected_roles[: len(actual_roles)]
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.agent_execution_records",
        )
    if assignments is None:
        assignment_by_id = {
            record.assignment_id: AgentAssignment.model_validate(
                assignment_templates[record.agent_role]
                .model_copy(
                    update={"assignment_id": record.assignment_id},
                    deep=True,
                )
                .model_dump(mode="python")
            )
            for record in report.agent_execution_records
            if record.agent_role in assignment_templates
        }
    else:
        assignment_ledger = tuple(assignments)
        ledger_ids = [assignment.assignment_id for assignment in assignment_ledger]
        ledger_roles = [assignment.agent_role for assignment in assignment_ledger]
        if (
            len(assignment_ledger) != len(assignment_template_sequence)
            or len(set(ledger_ids)) != len(ledger_ids)
            or ledger_roles != expected_roles
            or any(
                _CONFIRMED_REVIEW_ASSIGNMENT_ID.fullmatch(assignment.assignment_id) is None
                or assignment.agent_role != template.agent_role
                or assignment.task != template.task
                or assignment.read_only != template.read_only
                for assignment, template in zip(
                    assignment_ledger,
                    assignment_template_sequence,
                    strict=True,
                )
            )
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "assignments.json",
            )
        assignment_by_id = {
            assignment.assignment_id: assignment for assignment in assignment_ledger
        }
    if assignments is not None and agent_reports is None:
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "agent_reports",
        )
    report_sequence = tuple(agent_reports or ())
    report_ids = [agent_report.report_id for agent_report in report_sequence]
    if agent_reports is not None and (
        len(report_sequence) != len(report.agent_execution_records)
        or len(set(report_ids)) != len(report_ids)
        or [agent_report.agent_role for agent_report in report_sequence] != actual_roles
    ):
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "agent_reports",
        )
    if agent_reports is not None:
        for agent_report, record in zip(
            report_sequence,
            report.agent_execution_records,
            strict=True,
        ):
            assignment = assignment_by_id.get(record.assignment_id)
            if (
                assignment is None
                or agent_report.agent_role != assignment.agent_role
                or agent_report.task != assignment.task
                or agent_report.conclusions
                or agent_report.claims
                or agent_report.assumptions
                or agent_report.evidence_ids
                or agent_report.tool_result_ids
                or agent_report.formulas
                or agent_report.objections
                or agent_report.falsification_conditions
                or agent_report.confidence not in {ConfidenceGrade.C, ConfidenceGrade.D}
            ):
                _fail(
                    ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                    "agent_reports",
                )
        corrections = [
            claim for claim in report.claim_assessments if is_verified_claim_correction(claim)
        ]
        failed_tools = [
            result for result in report.tool_results if result.status is ToolStatus.FAILED
        ]
        successful_tools = [
            result for result in report.tool_results if result.status is ToolStatus.SUCCESS
        ]
        if any(event.blocked for event in report.security_events):
            projected_conclusion = (
                "このフレームワークは事後検討専用です。"
                "禁止用途に該当するため分析を実行しませんでした。"
            )
        elif report.run_status == "failed_with_limitations":
            projected_conclusion = (
                "実行予算または安全上の制限に達したため、制限付きで終了しました。"
            )
        elif corrections:
            projected_conclusion = "ユーザー主張に、再現可能なローカル計算に基づく訂正が必要です。"
        elif admission.case.kind == "hand" and report.data_quality:
            projected_conclusion = "ハンド入力に矛盾または不足があるため、戦略結論を断定しません。"
        elif failed_tools:
            projected_conclusion = "一部の計算が失敗したため、利用可能な結果と制限だけを返します。"
        elif successful_tools:
            projected_conclusion = "指定されたローカル検証・計算を完了しました。"
        else:
            projected_conclusion = (
                "正確な結論に必要な検証入力が不足しているため、断定を保留します。"
            )
        projected_sections = [
            {
                "title": agent_report.agent_role,
                "epistemic_status": EpistemicLabel.UNKNOWN.value,
                "unverified_conclusions": agent_report.conclusions,
                "unverified_claims": [claim.text for claim in agent_report.claims],
                "uncertainties": agent_report.uncertainties,
                "objections": agent_report.objections,
                "unresolved_questions": agent_report.unresolved_questions,
            }
            for agent_report in report_sequence
        ]
        if (
            report.conclusion != projected_conclusion
            or report.analysis_sections != projected_sections
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.conclusion",
            )
    assignment_is_authoritative = assignments is not None
    registered_tools = frozenset(default_registry().names())
    previous_completed_at: datetime | None = None
    for record_index, record in enumerate(report.agent_execution_records):
        expected_allowed_tools = (
            list(admission.case.requested_tools) if record.agent_role == "math-auditor" else []
        )
        assignment_template = assignment_templates.get(record.agent_role)
        assignment = assignment_by_id.get(record.assignment_id)
        if (
            assignment_template is None
            or assignment is None
            or assignment.agent_role != record.agent_role
            or _CONFIRMED_REVIEW_EXECUTION_ID.fullmatch(record.execution_id) is None
            or _CONFIRMED_REVIEW_ASSIGNMENT_ID.fullmatch(record.assignment_id) is None
            or record.context_id is None
            or _CONFIRMED_REVIEW_CONTEXT_ID.fullmatch(record.context_id) is None
            or record.context_attempt_id is None
            or _CONFIRMED_REVIEW_CONTEXT_ATTEMPT_ID.fullmatch(record.context_attempt_id) is None
            or record.provider != "local"
            or record.provider_version != "1.0.0"
            or record.model is not None
            or record.reasoning_effort is not None
            or record.allowed_tools != expected_allowed_tools
            or record.context_schema_version != "1.0.0"
            or record.context_classification != "internal"
            or record.context_payload_sha256 is None
            or record.context_source_sha256 is None
            or record.context_policy_sha256 is None
            or record.context_envelope_sha256 is None
            or record.context_expires_at is None
            or record.context_producer_runtime != "python-local"
            or record.context_consumer_runtime != "python-local"
        ):
            _fail(ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER, "report.agent_execution_records")
        if (
            record.started_at.tzinfo is None
            or record.started_at.utcoffset() is None
            or record.completed_at.tzinfo is None
            or record.completed_at.utcoffset() is None
            or record.started_at < admission.admitted_at
            or record.completed_at < record.started_at
            or record.completed_at > report.generated_at
            or record.context_expires_at.tzinfo is None
            or record.context_expires_at.utcoffset() is None
            or record.started_at > record.context_expires_at
            or record.context_expires_at - record.started_at
            > timedelta(seconds=_CONFIRMED_REVIEW_CONTEXT_MAX_DURATION_SECONDS)
            or (
                record.status.value == "completed"
                and record.completed_at > record.context_expires_at
            )
            or (previous_completed_at is not None and record.started_at < previous_completed_at)
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.agent_execution_records",
            )
        expected_context_fields = _expected_agent_context_fields(
            admission=admission,
            report=report,
            record=record,
            assignment=assignment,
            assignment_template=assignment_template,
            assignment_is_authoritative=assignment_is_authoritative,
            registered_tools=registered_tools,
        )
        if any(
            getattr(record, field) != expected
            for field, expected in expected_context_fields.items()
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.agent_execution_records",
            )
        if (record.status.value == "completed") != (record.error is None) or (
            record.error is not None
            and (not record.error.strip() or redact_sensitive(record.error) != record.error)
        ):
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.agent_execution_records",
            )
        record_commitment: dict[str, Any] = {
            "execution_record": record.model_dump(mode="json"),
        }
        if agent_reports is not None:
            record_commitment["agent_report"] = report_sequence[record_index].model_dump(
                mode="json"
            )
        agents.append(
            ConfirmedReviewAgentSupportV1(
                execution_id=record.execution_id,
                agent_role=record.agent_role,
                provider="local",
                provider_version="1.0.0",
                status=record.status.value,
                record_sha256=_domain_sha256(
                    PROVENANCE_CANONICALIZATION_ID + ":agent-record",
                    record_commitment,
                ),
            )
        )
        previous_completed_at = record.completed_at
        if report.run_status == "completed" and record.status.value != "completed":
            _fail(
                ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
                "report.agent_execution_records",
            )
    if assignments is None or agent_reports is None:
        _fail(
            ConfirmedReviewDiagnosticCode.REPORT_OVERREACH,
            "report.authority_artifacts",
        )
    provisional = ConfirmedReviewProvenanceV1(
        run_id=report.run_id,
        intake_id=admission.candidate.projection.candidate_input.intake_id,
        admitted_at=admission.admitted_at,
        source_sha256=admission.candidate.projection.source.content_sha256,
        candidate_sha256=admission.candidate.candidate_sha256,
        confirmation_sha256=admission.confirmation.confirmation_sha256,
        case_input_sha256=_domain_sha256(
            PROVENANCE_CANONICALIZATION_ID + ":case-input",
            admission.case.model_dump(mode="json"),
        ),
        final_report_sha256=_domain_sha256(
            PROVENANCE_CANONICALIZATION_ID + ":final-report",
            report.model_dump(mode="json"),
        ),
        agent_support=tuple(agents),
        tool_support=tool_support,
        terminal_status=report.run_status,
        provenance_sha256="0" * 64,
    )
    return provisional.model_copy(update={"provenance_sha256": provenance_sha256(provisional)})


def verify_confirmed_review_provenance(
    *,
    source_bytes: bytes,
    candidate: ReviewIntakeCandidateV1,
    confirmation: ReviewIntakeConfirmationV1,
    case: CaseInput,
    report: FinalReport,
    provenance: ConfirmedReviewProvenanceV1,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
) -> None:
    """Replay every durable source-to-report binding without provider execution."""

    provenance = _strict_provenance(provenance)
    admission = _admit_confirmed_review_at(
        source_bytes,
        candidate,
        confirmation,
        admitted_at=provenance.admitted_at,
    )
    if admission.case != case:
        _fail(ConfirmedReviewDiagnosticCode.STORAGE, "input.json")
    expected = build_confirmed_review_provenance(
        admission,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
    )
    if provenance != expected:
        _fail(ConfirmedReviewDiagnosticCode.STORAGE, "confirmed_review_provenance.json")


def default_confirmation_ids() -> tuple[str, str]:
    suffix = uuid4().hex
    return f"confirmation-{suffix[:12]}", f"idempotency-{suffix[12:24]}"


def review_confirmed_intake(
    admission: ConfirmedReviewAdmission,
    *,
    config: object | None = None,
) -> FinalReport:
    """Run the public local-only product path without provider/registry injection."""

    # Local imports keep the versioned contract layer independent from the
    # orchestration module and make it impossible for this API to accept an
    # alternate provider or tool registry.
    from poker_deliberation.config import AppConfig
    from poker_deliberation.orchestrator import Orchestrator
    from poker_deliberation.providers import LocalProvider

    if config is not None and not isinstance(config, AppConfig):
        raise TypeError("config must be AppConfig")
    orchestrator = Orchestrator(
        config=config,
        provider=LocalProvider(),
    )
    return orchestrator.run_confirmed_review(admission)
