from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.bounded_natural_language import (
    BoundedNaturalLanguageAdmission,
    admit_bounded_natural_language_review,
    create_bounded_confirmation,
    prepare_bounded_natural_language_intake,
)
from poker_deliberation.bounded_natural_language_models import (
    BoundedConfirmationAuthorityV1,
    BoundedIntakePreparationResultV1,
)
from poker_deliberation.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "tests" / "fixtures" / "bounded_natural_language" / "v1" / "valid-ja.txt"
SOURCE_BYTES = SOURCE_PATH.read_bytes()


def ready_bounded_preparation(
    *,
    source_bytes: bytes = SOURCE_BYTES,
    intake_id: str = "intake-bounded-test-1",
) -> BoundedIntakePreparationResultV1:
    result = prepare_bounded_natural_language_intake(
        source_bytes,
        intake_id=intake_id,
        source_id="fixture-bounded-test-1",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert result.status == "ready"
    assert result.source is not None
    assert result.candidate is not None
    return result


def bounded_admission(
    *,
    run_id: str = "run-bounded-test-1",
    source_bytes: bytes = SOURCE_BYTES,
    intake_id: str = "intake-bounded-test-1",
    now: datetime | None = None,
) -> BoundedNaturalLanguageAdmission:
    prepared = ready_bounded_preparation(
        source_bytes=source_bytes,
        intake_id=intake_id,
    )
    assert prepared.source is not None
    assert prepared.candidate is not None
    candidate = prepared.candidate
    projection = candidate.projection
    confirmation = create_bounded_confirmation(
        candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=BoundedConfirmationAuthorityV1(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=candidate.candidate_sha256,
        expected_source_bindings_sha256=projection.source_bindings_sha256,
        expected_focal_sha256=projection.focal_decision.focal_sha256,
        expected_tool_plan_sha256=projection.tool_plan.tool_plan_sha256,
        expected_extractor_sha256=projection.extractor_sha256,
        confirmed_at=now or datetime.now(UTC),
    )
    return admit_bounded_natural_language_review(source_bytes, candidate, confirmation)


def app_config(root: Path) -> AppConfig:
    return AppConfig(
        runs_dir=root / "legacy",
        revision_runs_dir=root / "product",
        durable_budget_runs_dir=root / "budget",
    )
