from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError, create_model

from poker_deliberation.bounded_natural_language import (
    BoundedNaturalLanguageError,
    admit_bounded_natural_language_review,
    create_bounded_confirmation,
    prepare_bounded_natural_language_intake,
    verify_bounded_candidate,
)
from poker_deliberation.bounded_natural_language_models import (
    BoundedCandidateProjectionV1,
    BoundedConfirmationAuthorityV1,
    BoundedIntakeCandidateV1,
    BoundedNaturalLanguageDiagnosticCode,
)
from tests.bounded_natural_language_support import (
    SOURCE_BYTES,
    focal_call_source,
    multiplayer_source,
    ready_bounded_preparation,
)


def _prepare(source: bytes):
    return prepare_bounded_natural_language_intake(
        source,
        intake_id="intake-unit-1",
        source_id="fixture-unit-1",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )


def _confirmation(prepared, *, run_id: str = "run-bounded-unit-1", now=None):
    assert prepared.source is not None and prepared.candidate is not None
    candidate = prepared.candidate
    projection = candidate.projection
    return create_bounded_confirmation(
        candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=BoundedConfirmationAuthorityV1(
            authority_id="local-unit-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_source_sha256=prepared.source.content_sha256,
        expected_candidate_sha256=candidate.candidate_sha256,
        expected_source_bindings_sha256=projection.source_bindings_sha256,
        expected_focal_sha256=projection.focal_decision.focal_sha256,
        expected_tool_plan_sha256=projection.tool_plan.tool_plan_sha256,
        expected_extractor_sha256=projection.extractor_sha256,
        confirmed_at=now,
    )


def test_valid_fixture_extracts_exact_hand_focal_and_tool_plan() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    projection = prepared.candidate.projection

    assert projection.hand.table_size == 2
    assert projection.hand.hero_cards == ["As", "Kd"]
    assert len(projection.hand.actions) == 9
    assert projection.focal_decision.model_dump(mode="json") | {"focal_sha256": "ignored"} == {
        "selector_street": "turn",
        "selector_actor": "Villain",
        "selector_action": "bet",
        "selector_amount": 8.0,
        "facing_action_index": 7,
        "hero_action_index": 8,
        "hero_response": "fold",
        "focal_sha256": "ignored",
    }
    assert projection.tool_plan.ordered_tools == (
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
    )
    assert projection.tool_plan.pot_odds_input.model_dump(mode="json") == {
        "pot_before_bet": 12.0,
        "opponent_bet": 8.0,
        "call_cost": 8.0,
        "expected_rake": 0.0,
    }
    assert (
        projection.tool_plan.pot_before_bet_units,
        projection.tool_plan.opponent_bet_units,
        projection.tool_plan.call_cost_units,
        projection.tool_plan.contestable_pot_units,
    ) == (12, 8, 8, 28)


def test_every_extracted_binding_is_an_exact_utf8_half_open_source_span() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.source is not None and prepared.candidate is not None
    bindings = prepared.candidate.projection.source_bindings

    assert bindings
    assert len({item.field_path for item in bindings}) == len(bindings)
    assert all(item.source_sha256 == prepared.source.content_sha256 for item in bindings)
    assert all(SOURCE_BYTES[item.start_byte : item.end_byte] for item in bindings)
    by_path = {item.field_path: item for item in bindings}
    assert (
        SOURCE_BYTES[
            by_path["hand.hero_cards[0]"].start_byte : by_path["hand.hero_cards[0]"].end_byte
        ]
        == b"As"
    )
    assert (
        SOURCE_BYTES[
            by_path["focal_decision.selector_amount"].start_byte : by_path[
                "focal_decision.selector_amount"
            ].end_byte
        ]
        == b"8"
    )
    assert (
        SOURCE_BYTES[
            by_path["hand.actions[0].action"].start_byte : by_path[
                "hand.actions[0].action"
            ].end_byte
        ].decode()
        == "ポスト"
    )
    assert (
        SOURCE_BYTES[
            by_path["hand.actions[1].action"].start_byte : by_path[
                "hand.actions[1].action"
            ].end_byte
        ].decode()
        == "ポスト"
    )
    assert (
        SOURCE_BYTES[
            by_path["hand.actions[0].blind"].start_byte : by_path["hand.actions[0].blind"].end_byte
        ]
        == b"SB"
    )
    assert (
        SOURCE_BYTES[
            by_path["hand.actions[1].blind"].start_byte : by_path["hand.actions[1].blind"].end_byte
        ]
        == b"BB"
    )


@pytest.mark.parametrize(
    ("table_size", "expected_positions", "facing_index", "hero_index"),
    [
        (3, ["BTN", "SB", "BB"], 8, 9),
        (6, ["UTG", "HJ", "CO", "BTN", "SB", "BB"], 11, 12),
    ],
)
def test_three_and_six_player_boundaries_extract_without_false_side_pot_rejection(
    table_size: int,
    expected_positions: list[str],
    facing_index: int,
    hero_index: int,
) -> None:
    prepared = ready_bounded_preparation(
        source_bytes=multiplayer_source(table_size),
        intake_id=f"intake-bounded-{table_size}-player",
    )
    assert prepared.candidate is not None
    projection = prepared.candidate.projection
    assert projection.hand.table_size == table_size
    assert [item.position for item in projection.hand.players] == expected_positions
    assert projection.focal_decision.facing_action_index == facing_index
    assert projection.focal_decision.hero_action_index == hero_index
    assert projection.tool_plan.contestable_pot_units == 28


def test_completed_hand_can_select_a_nonterminal_focal_call() -> None:
    prepared = ready_bounded_preparation(
        source_bytes=focal_call_source(),
        intake_id="intake-bounded-focal-call",
    )
    assert prepared.candidate is not None
    projection = prepared.candidate.projection
    assert projection.focal_decision.hero_response == "call"
    assert projection.focal_decision.facing_action_index == 7
    assert projection.focal_decision.hero_action_index == 8
    assert projection.hand.actions[-1].action == "fold"
    assert projection.tool_plan.pot_odds_input.model_dump(mode="json") == {
        "pot_before_bet": 12.0,
        "opponent_bet": 8.0,
        "call_cost": 8.0,
        "expected_rake": 0.0,
    }


def test_missing_header_is_not_misclassified_as_duplicate_blinds() -> None:
    source = b"\n".join(SOURCE_BYTES.splitlines()[1:]) + b"\n"
    result = _prepare(source)
    assert result.status == "blocked"
    assert [(item.code.value, item.field_path) for item in result.diagnostics] == [
        ("BNL_E_MISSING", "hand.header")
    ]


def test_declared_blind_kind_must_match_actor_position() -> None:
    source = SOURCE_BYTES.replace(
        "Heroが1をSBとしてポストしました。".encode(),
        "Heroが1をBBとしてポストしました。".encode(),
    )
    result = _prepare(source)
    assert result.status == "blocked"
    assert [(item.code.value, item.field_path) for item in result.diagnostics] == [
        ("BNL_E_CONFLICT", "hand.actions.post_blind")
    ]


@pytest.mark.parametrize(
    ("source", "code", "field_path"),
    [
        (
            SOURCE_BYTES.replace(
                "Villainがチェックしました。\nフロップ".encode(),
                "Villainがチェックしました。\nVillainがチェックしました。\nフロップ".encode(),
            ),
            "BNL_E_ACTION",
            "hand.actions.actor_order",
        ),
        (
            SOURCE_BYTES.replace(
                "Heroが1をコールしました。\nVillainがチェックしました。".encode(),
                "Villainがチェックしました。\nHeroが1をコールしました。".encode(),
            ),
            "BNL_E_ACTION",
            "hand.actions.actor_order",
        ),
        (
            SOURCE_BYTES.replace(
                "Heroがフォールドしました。\n判断直前".encode(),
                "Heroがフォールドしました。\nリバーは3hです。\n判断直前".encode(),
            ),
            "BNL_E_ACTION",
            "hand.actions.terminal",
        ),
        (
            SOURCE_BYTES.replace(
                "フロップはAh 7d 2cです。\nVillainがチェックしました。\n"
                "Heroが4をベットしました。\nVillainが4をコールしました。\n".encode(),
                "フロップはAh 7d 2cです。\n".encode(),
            ),
            "BNL_E_STREET",
            "hand.actions.street",
        ),
        (
            SOURCE_BYTES.replace(
                "HeroはSBで開始スタック100です。".encode(),
                "HeroはSBで開始スタック14です。".encode(),
            ),
            "BNL_E_UNSUPPORTED",
            "focal_decision.all_in",
        ),
        (
            SOURCE_BYTES.replace(
                "VillainはBBで開始スタック100です。".encode(),
                "VillainはBBで開始スタック999999999995.0003です。".encode(),
            ),
            "BNL_E_AMOUNT",
            "hand.players.starting_stack",
        ),
    ],
)
def test_action_order_terminal_all_in_and_decimal_boundaries_fail_closed(
    source: bytes,
    code: str,
    field_path: str,
) -> None:
    result = _prepare(source)
    assert result.status == "blocked"
    assert [(item.code.value, item.field_path) for item in result.diagnostics] == [
        (code, field_path)
    ]


@pytest.mark.parametrize(
    ("source", "code", "field_path"),
    [
        (b"", "BNL_E_SOURCE_SIZE", "source"),
        (b"\xef\xbb\xbf" + SOURCE_BYTES, "BNL_E_SOURCE_BOM", "source"),
        (SOURCE_BYTES.replace(b"\n", b"\r\n"), "BNL_E_SOURCE_NEWLINE", "source"),
        (b"\xff", "BNL_E_SOURCE_UTF8", "source"),
        (
            SOURCE_BYTES.replace(b"Hero", "He\u0301ro".encode()),
            "BNL_E_SOURCE_NFC",
            "source",
        ),
        (
            SOURCE_BYTES.replace(b"Hero", b"Hero\x00", 1),
            "BNL_E_SOURCE_CONTROL",
            "source",
        ),
    ],
)
def test_source_boundary_diagnostics_are_exact(source: bytes, code: str, field_path: str) -> None:
    result = _prepare(source)
    assert result.status == "blocked"
    assert [(item.code.value, item.field_path) for item in result.diagnostics] == [
        (code, field_path)
    ]


def test_missing_focal_and_ambiguous_raise_are_not_inferred() -> None:
    missing = SOURCE_BYTES.rsplit("検討対象は".encode(), 1)[0]
    missing_result = _prepare(missing)
    assert missing_result.status == "blocked"
    assert missing_result.diagnostics[0].code is BoundedNaturalLanguageDiagnosticCode.FOCAL_MISSING

    ambiguous = SOURCE_BYTES.replace(
        "Heroがフォールドしました。".encode(),
        "Heroが8へレイズしました。".encode(),
    )
    ambiguous_result = _prepare(ambiguous)
    assert ambiguous_result.status == "blocked"
    assert (
        ambiguous_result.diagnostics[0].code is BoundedNaturalLanguageDiagnosticCode.RAISE_AMBIGUITY
    )


def test_declared_pot_mismatch_fails_closed() -> None:
    result = _prepare(
        SOURCE_BYTES.replace(
            "判断直前のポットは12".encode(),
            "判断直前のポットは13".encode(),
        )
    )
    assert result.status == "blocked"
    assert result.diagnostics[0].code is BoundedNaturalLanguageDiagnosticCode.POT_MISMATCH
    assert result.diagnostics[0].field_path == "declared_pot_assertions.pot_before_bet"


def test_confirmation_binds_all_six_hashes_and_raw_text_never_enters_case() -> None:
    prepared = ready_bounded_preparation()
    confirmation = _confirmation(prepared)
    assert prepared.candidate is not None
    admission = admit_bounded_natural_language_review(
        SOURCE_BYTES, prepared.candidate, confirmation
    )
    projection = prepared.candidate.projection

    assert admission.case.raw_text is None
    assert admission.case.requested_tools == list(projection.tool_plan.ordered_tools)
    assert admission.confirmation.source_sha256 == projection.source.content_sha256
    assert admission.confirmation.candidate_sha256 == prepared.candidate.candidate_sha256
    assert admission.confirmation.source_bindings_sha256 == projection.source_bindings_sha256
    assert admission.confirmation.focal_sha256 == projection.focal_decision.focal_sha256
    assert admission.confirmation.tool_plan_sha256 == projection.tool_plan.tool_plan_sha256
    assert admission.confirmation.extractor_sha256 == projection.extractor_sha256
    assert admission.case.claims[0].label.value == "USER_CLAIM"


def test_candidate_hash_tamper_and_unknown_fields_are_rejected() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    forged = prepared.candidate.model_copy(update={"candidate_sha256": "0" * 64})
    with pytest.raises(BoundedNaturalLanguageError) as error:
        verify_bounded_candidate(forged)
    assert error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING

    payload = prepared.candidate.model_dump(mode="python") | {"future": True}
    with pytest.raises(ValidationError):
        BoundedIntakeCandidateV1.model_validate(payload)

    secret = "sk-" + "d" * 26
    copied_unknown = prepared.candidate.model_copy(update={"future": secret})
    with pytest.raises(BoundedNaturalLanguageError) as copied_error:
        verify_bounded_candidate(copied_unknown)
    assert copied_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFLICT
    assert secret not in str(copied_error.value)


def test_candidate_type_tamper_and_cycle_are_rejected_without_warnings() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    secret = "sk-" + "f" * 26
    cycle: list[object] = []
    cycle.append(cycle)

    for forged_projection in (secret, cycle):
        forged = prepared.candidate.model_copy(update={"projection": forged_projection})
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            with pytest.raises(BoundedNaturalLanguageError) as error:
                verify_bounded_candidate(forged)
        assert error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFLICT
        assert str(error.value) == "BNL_E_CONFLICT"
        assert observed == []


def test_nested_model_subclasses_are_rejected_without_silent_field_loss() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    projection_type = create_model(
        "ProjectionWithFutureUnit",
        future=(str, ...),
        __base__=BoundedCandidateProjectionV1,
    )
    nested_projection = projection_type.model_validate(
        prepared.candidate.projection.model_dump(mode="python")
        | {"future": "opaque-candidate-marker"}
    )
    forged_candidate = prepared.candidate.model_copy(update={"projection": nested_projection})

    with warnings.catch_warnings(record=True) as candidate_warnings:
        warnings.simplefilter("always")
        with pytest.raises(BoundedNaturalLanguageError) as candidate_error:
            verify_bounded_candidate(forged_candidate)
    assert candidate_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFLICT
    assert candidate_warnings == []

    confirmation = _confirmation(prepared)
    authority_type = create_model(
        "AuthorityWithFutureUnit",
        future=(str, ...),
        __base__=BoundedConfirmationAuthorityV1,
    )
    nested_authority = authority_type.model_validate(
        confirmation.authority.model_dump(mode="python") | {"future": "opaque-authority-marker"}
    )
    forged_confirmation = confirmation.model_copy(update={"authority": nested_authority})
    with warnings.catch_warnings(record=True) as confirmation_warnings:
        warnings.simplefilter("always")
        with pytest.raises(BoundedNaturalLanguageError) as confirmation_error:
            admit_bounded_natural_language_review(
                SOURCE_BYTES,
                prepared.candidate,
                forged_confirmation,
            )
    assert (
        confirmation_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING
    )
    assert confirmation_warnings == []


def test_stale_confirmation_and_cross_run_binding_are_rejected() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    now = datetime.now(UTC)
    stale = _confirmation(prepared, now=now - timedelta(days=2))
    with pytest.raises(BoundedNaturalLanguageError) as stale_error:
        admit_bounded_natural_language_review(SOURCE_BYTES, prepared.candidate, stale)
    assert stale_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_EXPIRED

    current = _confirmation(prepared, run_id="run-bounded-unit-a", now=now)
    forged = current.model_copy(update={"run_id": "run-bounded-unit-b"})
    with pytest.raises(BoundedNaturalLanguageError) as replay_error:
        admit_bounded_natural_language_review(SOURCE_BYTES, prepared.candidate, forged)
    assert replay_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING

    secret = "sk-" + "e" * 26
    copied_unknown = current.model_copy(update={"future": secret})
    with pytest.raises(BoundedNaturalLanguageError) as copied_error:
        admit_bounded_natural_language_review(
            SOURCE_BYTES,
            prepared.candidate,
            copied_unknown,
        )
    assert copied_error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING
    assert secret not in str(copied_error.value)


def test_confirmation_known_field_type_tamper_is_sanitized_without_warnings() -> None:
    prepared = ready_bounded_preparation()
    assert prepared.candidate is not None
    secret = "sk-" + "g" * 26
    confirmation = _confirmation(prepared)
    forged = confirmation.model_copy(update={"authority": secret})

    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        with pytest.raises(BoundedNaturalLanguageError) as error:
            admit_bounded_natural_language_review(
                SOURCE_BYTES,
                prepared.candidate,
                forged,
            )
    assert error.value.code is BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING
    assert str(error.value) == "BNL_E_CONFIRMATION_BINDING"
    assert secret not in str(error.value)
    assert observed == []
