from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.bounded_natural_language import prepare_bounded_natural_language_intake
from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvAdmission,
    admit_bounded_river_call_ev_review,
    create_bounded_river_call_ev_authority,
    create_bounded_river_call_ev_confirmation,
    prepare_bounded_river_call_ev_intake,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvPreparationResultV1,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from tests.bounded_natural_language_support import multiplayer_source

ROOT = Path(__file__).resolve().parents[1]
BASE_SOURCE = (
    ROOT / "tests" / "fixtures" / "bounded_natural_language" / "v1" / "valid-ja.txt"
).read_bytes()


def river_source() -> bytes:
    source = BASE_SOURCE.replace(
        "Heroがフォールドしました。".encode(),
        (
            "Heroが8をコールしました。\n"
            "リバーは3hです。\n"
            "Villainが10をベットしました。\n"
            "Heroがフォールドしました。"
        ).encode(),
        1,
    )
    return source.replace(
        (
            "判断直前のポットは12です。\n"
            "コール額は8です。\n"
            "コール後の争点ポットは28です。\n"
            "検討対象は、ターンでVillainが8をベットした直後のHeroのコールまたは"
            "フォールド判断です。"
        ).encode(),
        (
            "判断直前のポットは28です。\n"
            "コール額は10です。\n"
            "コール後の争点ポットは48です。\n"
            "検討対象は、リバーでVillainが10をベットした直後のHeroのコールまたは"
            "フォールド判断です。"
        ).encode(),
        1,
    )


def multiplayer_river_source(table_size: int) -> bytes:
    source = multiplayer_source(table_size).replace(
        "Heroがフォールドしました。".encode(),
        (
            "Heroが8をコールしました。\n"
            "リバーは3hです。\n"
            "Heroがチェックしました。\n"
            "Villainが10をベットしました。\n"
            "Heroがフォールドしました。"
        ).encode(),
        1,
    )
    return source.replace(
        (
            "判断直前のポットは12です。\n"
            "コール額は8です。\n"
            "コール後の争点ポットは28です。\n"
            "検討対象は、ターンでVillainが8をベットした直後のHeroのコールまたは"
            "フォールド判断です。"
        ).encode(),
        (
            "判断直前のポットは28です。\n"
            "コール額は10です。\n"
            "コール後の争点ポットは48です。\n"
            "検討対象は、リバーでVillainが10をベットした直後のHeroのコールまたは"
            "フォールド判断です。"
        ).encode(),
        1,
    )


def range_definition(
    source_bytes: bytes,
    notation: str = "QcJc",
    *,
    content_status: str = "ASSUMPTION",
) -> VersionedRangeDefinitionV1:
    bounded = prepare_bounded_natural_language_intake(
        source_bytes,
        intake_id="intake-river-support",
        source_id="fixture-river-support",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert bounded.status == "ready"
    assert bounded.candidate is not None
    hand = bounded.candidate.projection.hand
    focal = bounded.candidate.projection.focal_decision
    return VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": "villain-river-call-ev",
            "target_player_id": "Villain",
            "notation": notation,
            "source": {
                "source_id": "river-call-ev-fixture",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": content_status,
                "content_sha256": hashlib.sha256(notation.encode()).hexdigest(),
            },
            "game_conditions": {
                "game_type": "NLHE",
                "format": "cash",
                "table_size": hand.table_size,
                "target_position": "BB",
                "street": "river",
                "starting_stack_min_bb_milli": 50_000,
                "starting_stack_max_bb_milli": 50_000,
                "as_of_action_index": focal.facing_action_index + 1,
                "action_prefix_sha256": action_prefix_sha256(
                    hand,
                    focal.facing_action_index + 1,
                ),
            },
        }
    )


def ready_preparation(
    *,
    notation: str = "QcJc",
    source_bytes: bytes | None = None,
    intake_id: str = "intake-river-test-1",
) -> BoundedRiverCallEvPreparationResultV1:
    source = source_bytes or river_source()
    definition = range_definition(source, notation)
    prepared = prepare_bounded_river_call_ev_intake(
        source,
        definition,
        intake_id=intake_id,
        source_id="fixture-river-test-1",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert prepared.status == "ready", prepared
    assert prepared.candidate is not None
    return prepared


def candidate_hashes(candidate: BoundedRiverCallEvCandidateV1) -> tuple[str, ...]:
    projection = candidate.projection
    return (
        projection.source_sha256,
        projection.bounded_candidate_sha256,
        projection.source_bindings_sha256,
        projection.focal_sha256,
        projection.extractor_sha256,
        projection.tool_plan_sha256,
        projection.range_definition_sha256,
        projection.range_target_sha256,
        projection.range_binding_sha256,
        projection.equity_model_sha256,
        projection.call_ev_model_sha256,
        candidate.candidate_sha256,
    )


def admission(
    *,
    run_id: str = "run-river-test-1",
    notation: str = "QcJc",
    source_bytes: bytes | None = None,
    now: datetime | None = None,
) -> BoundedRiverCallEvAdmission:
    source = source_bytes or river_source()
    prepared = ready_preparation(
        notation=notation,
        source_bytes=source,
        intake_id=f"intake-{run_id}",
    )
    assert prepared.candidate is not None
    candidate = prepared.candidate
    confirmation = create_bounded_river_call_ev_confirmation(
        candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=create_bounded_river_call_ev_authority(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_hashes=candidate_hashes(candidate),
        confirmed_at=now or datetime.now(UTC),
    )
    return admit_bounded_river_call_ev_review(source, candidate, confirmation)


def app_config(root: Path) -> AppConfig:
    return AppConfig(
        runs_dir=root / "legacy",
        revision_runs_dir=root / "product",
        durable_budget_runs_dir=root / "budget",
    )
