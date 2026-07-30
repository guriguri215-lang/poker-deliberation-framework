from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poker_deliberation.config import AppConfig
from poker_deliberation.confirmed_review import (
    ConfirmedReviewAdmission,
    admit_confirmed_review,
    create_review_confirmation,
    prepare_review_intake,
)
from poker_deliberation.confirmed_review_models import (
    ReviewConfirmationAuthorityV1,
    ReviewIntakePreparationResultV1,
)

SOURCE_BYTES = b"Hero raised preflop and Villain folded.\n"


def candidate_payload(*, intake_id: str = "intake-test-1") -> dict[str, Any]:
    return {
        "intake_id": intake_id,
        "hand": {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 2,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {
                    "player_id": "hero",
                    "position": "SB",
                    "starting_stack": 100,
                },
                {
                    "player_id": "villain",
                    "position": "BB",
                    "starting_stack": 100,
                },
            ],
            "hero_player_id": "hero",
            "hero_cards": ["As", "Kh"],
            "actions": [
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "post_blind",
                    "amount": 1,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "post_blind",
                    "amount": 2,
                },
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "raise",
                    "amount": 5,
                    "to_amount": 6,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "fold",
                    "amount": 0,
                },
            ],
        },
        "ambiguities": [],
        "claims": [
            {
                "claim_id": "claim-test-1",
                "text": "The preflop raise was the best action.",
                "label": "USER_CLAIM",
                "confidence": "C",
            }
        ],
    }


def ready_preparation(
    *,
    source_bytes: bytes = SOURCE_BYTES,
    payload: object | None = None,
    source_id: str = "source-test-1",
) -> ReviewIntakePreparationResultV1:
    result = prepare_review_intake(
        source_bytes,
        candidate_payload() if payload is None else payload,
        source_id=source_id,
        source_kind="user_supplied",
        license_classification="user_supplied_private_analysis",
        usage_classification="local_analysis_only",
        classification="internal",
    )
    assert result.status == "ready"
    assert result.source is not None
    assert result.candidate is not None
    return result


def confirmed_admission(
    *,
    run_id: str = "run-confirmed-test-1",
    source_bytes: bytes = SOURCE_BYTES,
    payload: object | None = None,
    now: datetime | None = None,
) -> ConfirmedReviewAdmission:
    prepared = ready_preparation(source_bytes=source_bytes, payload=payload)
    assert prepared.source is not None
    assert prepared.candidate is not None
    confirmation_time = now or datetime.now(UTC)
    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    confirmation = create_review_confirmation(
        prepared.candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=authority,
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=prepared.candidate.candidate_sha256,
        confirmed_at=confirmation_time,
    )
    return admit_confirmed_review(
        source_bytes,
        prepared.candidate,
        confirmation,
    )


def app_config(root: Path) -> AppConfig:
    return AppConfig(
        runs_dir=root / "legacy",
        revision_runs_dir=root / "product",
        durable_budget_runs_dir=root / "budget",
    )
