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


def focal_call_source() -> bytes:
    """Complete the hand after a turn focal call so the call branch is retrospective."""

    return SOURCE_BYTES.replace(
        "Heroがフォールドしました。".encode(),
        (
            "Heroが8をコールしました。\n"
            "リバーは3hです。\n"
            "Villainが10をベットしました。\n"
            "Heroがフォールドしました。"
        ).encode(),
        1,
    )


def multiplayer_source(table_size: int) -> bytes:
    """Build a legal 3- or 6-handed boundary fixture with the same focal decision."""

    positions = {
        3: (("SeatBTN", "BTN"), ("Hero", "SB"), ("Villain", "BB")),
        6: (
            ("SeatUTG", "UTG"),
            ("SeatHJ", "HJ"),
            ("SeatCO", "CO"),
            ("SeatBTN", "BTN"),
            ("Hero", "SB"),
            ("Villain", "BB"),
        ),
    }
    if table_size not in positions:
        raise ValueError("table_size must be 3 or 6")
    players = positions[table_size]
    preflop_folds = [
        f"{player}がフォールドしました。"
        for player, position in players
        if position not in {"SB", "BB"}
    ]
    lines = [
        f"これは完了済みのNLHEキャッシュゲームです。参加者は{table_size}人です。",
        "ブラインドは1/2で、アンティは0、レーキは0です。",
        *(f"{player}は{position}で開始スタック100です。" for player, position in players),
        "HeroのホールカードはAs Kdです。",
        "プリフロップです。",
        "Heroが1をSBとしてポストしました。",
        "Villainが2をBBとしてポストしました。",
        *preflop_folds,
        "Heroが1をコールしました。",
        "Villainがチェックしました。",
        "フロップはAh 7d 2cです。",
        "Heroが4をベットしました。",
        "Villainが4をコールしました。",
        "ターンは9sです。",
        "Heroがチェックしました。",
        "Villainが8をベットしました。",
        "Heroがフォールドしました。",
        "判断直前のポットは12です。",
        "コール額は8です。",
        "コール後の争点ポットは28です。",
        "検討対象は、ターンでVillainが8をベットした直後のHeroのコールまたはフォールド判断です。",
    ]
    return ("\n".join(lines) + "\n").encode()


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
