"""Calculator-free semantic read of one successful P3-030C terminal revision."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TypeGuard

from pydantic import BaseModel

from poker_deliberation.bounded_natural_language_models import (
    BOUNDED_NL_BINDINGS_CANONICALIZATION_ID,
    BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID,
    BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID,
    BOUNDED_NL_EXTRACTOR_ID,
    BOUNDED_NL_EXTRACTOR_VERSION,
    BOUNDED_NL_FOCAL_CANONICALIZATION_ID,
    BOUNDED_NL_SOURCE_CANONICALIZATION_ID,
    BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID,
)
from poker_deliberation.bounded_river_call_ev_models import (
    AUTHORITY_HASH_DOMAIN,
    BOUNDED_CANDIDATE_HASH_DOMAIN,
    BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    CALL_EV_MODEL_HASH_DOMAIN,
    CANDIDATE_HASH_DOMAIN,
    CONFIRMATION_HASH_DOMAIN,
    EQUITY_MODEL_HASH_DOMAIN,
    EXTRACTOR_HASH_DOMAIN,
    FOCAL_HASH_DOMAIN,
    RANGE_BINDING_HASH_DOMAIN,
    RANGE_DEFINITION_HASH_DOMAIN,
    RANGE_TARGET_HASH_DOMAIN,
    SOURCE_BINDINGS_HASH_DOMAIN,
    SOURCE_HASH_DOMAIN,
    TOOL_PLAN_HASH_DOMAIN,
    TOOL_RESULT_HASH_DOMAIN,
    BoundedRiverCallEvBindingV1,
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverCallEvProvenanceV1,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.range_equity_models import BINDING_HASH_DOMAIN as RANGE_HASH_DOMAIN
from poker_deliberation.range_equity_models import (
    CANDIDATE_HASH_DOMAIN as RANGE_CANDIDATE_HASH_DOMAIN,
)
from poker_deliberation.range_equity_models import (
    COMBOS_INPUT_HASH_DOMAIN,
    COMBOS_OUTPUT_HASH_DOMAIN,
    EQUITY_INPUT_HASH_DOMAIN,
    EQUITY_OUTPUT_HASH_DOMAIN,
    RANGE_EQUITY_BINDING_ARTIFACT,
    SOURCE_RANGE_HASH_DOMAIN,
    VALIDATION_INPUT_HASH_DOMAIN,
    VALIDATION_OUTPUT_HASH_DOMAIN,
    VersionedRangeRiverEquityBindingV1,
    canonical_domain_sha256,
)
from poker_deliberation.range_models import (
    RangeValidationResultV1,
    VersionedRangeDefinitionV1,
)
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentReport,
    CaseInput,
    FinalReport,
    ToolResult,
)
from poker_deliberation.storage.bounded_river_call_ev_admission_store import (
    read_bounded_river_call_ev_admission_record,
    verify_bounded_river_call_ev_admission_record,
)
from poker_deliberation.storage.range_equity_admission_store import (
    read_range_equity_admission_record,
    verify_range_equity_admission_record,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    parse_canonical_json,
    parse_canonical_model,
    parse_canonical_model_list,
)
from poker_deliberation.storage.terminal_models import RunReadStatus, VerifiedRunReadV2
from poker_deliberation.tools.contracts import (
    CombosOutput,
    HandValidatorOutput,
    HoldemEquityOutput,
    PotOddsOutput,
    RakedCallEVOutput,
)
from poker_deliberation.tools.hand_pot_ledger import HandPotLedgerOutputV1
from poker_deliberation.tools.numeric import close_ulps

_MAX_ADMISSION_RECORD_BYTES = 1_000_000
_TERMINAL_ROOT_HASH_DOMAIN = "poker-bounded-river-call-ev-terminal-revision-root-v1"
_P3_SUCCESS_ARTIFACTS = frozenset(
    {
        BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
    }
)
_P3_ARTIFACT_FAMILY = _P3_SUCCESS_ARTIFACTS | {BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT}
_TOOL_CONTRACT_METADATA = {
    "hand_validator": (
        "floating-verified",
        "declared canonical hand rules profile",
        None,
    ),
    "hand_pot_ledger": (
        "exact-under-model",
        "generic_nlhe_cash_no_rake_v1 version 1.0.0",
        None,
    ),
    "pot_odds": ("floating-verified", None, None),
    "range_validate": (
        "exact",
        "poker-deliberation.nlhe-range version 1.0.0",
        None,
    ),
    "combos": ("floating-verified", None, None),
    "holdem_equity": ("floating-verified", None, "exact_enumeration"),
    "raked_call_ev": (
        "floating-verified",
        "single decision, no future betting, declared final-pot rake",
        None,
    ),
}
_TOOL_OUTPUT_KEYS = {
    "hand_validator": frozenset(
        {
            "valid",
            "verification_tolerance",
            "errors",
            "warnings",
            "final_pot",
            "remaining_stacks",
            "reconstructed_actions",
            "decision_snapshots",
            "limitations",
        }
    ),
    "hand_pot_ledger": frozenset(
        {
            "schema_version",
            "profile_id",
            "profile_version",
            "supported_site",
            "chip_unit",
            "ledger_actions",
            "uncalled_returns",
            "pot_layers",
            "player_eligibility",
            "gross_contributions_units",
            "net_contributions_units",
            "remaining_stacks_units",
            "gross_committed_units",
            "total_returned_units",
            "final_pot_units",
            "starting_chips_units",
            "conservation_verified",
            "oracle_verified",
            "limitations",
        }
    ),
    "pot_odds": frozenset(
        {
            "pot_after_opponent_bet",
            "final_pot_before_rake",
            "expected_rake",
            "final_pot_after_rake",
            "required_equity",
            "required_equity_percent",
            "pot_odds_against",
        }
    ),
    "range_validate": frozenset(
        {
            "schema_version",
            "result_version",
            "grammar_id",
            "grammar_version",
            "hash_algorithm",
            "status",
            "range_id",
            "target_player_id",
            "source",
            "game_conditions",
            "source_notation_sha256",
            "condition_binding_sha256",
            "blockers",
            "diagnostics",
            "canonical_notation",
            "canonical_combo_sha256",
            "combos",
            "combo_count",
            "total_weight_millionths",
        }
    ),
    "combos": frozenset({"range", "combo_count", "total_combo_weight", "normalized_weights"}),
    "holdem_equity": frozenset(
        {
            "method",
            "exact",
            "hero_equity",
            "evaluations",
            "unweighted_wins",
            "unweighted_ties",
            "unweighted_losses",
            "range_pair_count",
            "cards_to_come",
        }
    ),
    "raked_call_ev": frozenset({"ev", "rake_amount", "final_pot_after_rake", "formula", "model"}),
}


class P3TerminalSourceReadError(ValueError):
    """A generic terminal read did not prove the complete P3-030C semantics."""


@dataclass(frozen=True, slots=True)
class VerifiedP3TerminalSourceV1:
    """Typed artifacts admitted by the P3-030C source-to-report replay."""

    source_bytes: bytes
    range_definition: VersionedRangeDefinitionV1
    candidate: BoundedRiverCallEvCandidateV1
    confirmation: BoundedRiverCallEvConfirmationV1
    binding: BoundedRiverCallEvBindingV1
    result: BoundedRiverCallEvResultV1
    provenance: BoundedRiverCallEvProvenanceV1
    range_equity_binding: VersionedRangeRiverEquityBindingV1
    case: CaseInput
    report: FinalReport


def _payload(read: VerifiedRunReadV2, logical_name: str) -> bytes:
    try:
        return read.payload_bytes(logical_name)
    except KeyError as exc:
        raise P3TerminalSourceReadError(
            f"P3-030C terminal artifact is missing: {logical_name}"
        ) from exc


def _ordered_agent_reports(
    read: VerifiedRunReadV2,
    report: FinalReport,
    logical_names: frozenset[str],
) -> tuple[AgentReport, ...]:
    report_names = tuple(
        sorted(
            (
                name
                for name in logical_names
                if name.startswith("agent_reports/") and name.endswith(".json")
            ),
            key=lambda value: value.encode("utf-8"),
        )
    )
    reports = tuple(
        parse_canonical_model(_payload(read, name), AgentReport) for name in report_names
    )
    reports_by_role = {item.agent_role: item for item in reports}
    try:
        ordered = tuple(reports_by_role[item.agent_role] for item in report.agent_execution_records)
    except KeyError as exc:
        raise P3TerminalSourceReadError(
            "P3-030C agent reports do not cover every execution record"
        ) from exc
    if len(reports_by_role) != len(reports) or len(ordered) != len(reports):
        raise P3TerminalSourceReadError("P3-030C agent reports are not an exact role ledger")
    return ordered


def _verify_tool_artifacts(
    read: VerifiedRunReadV2,
    report: FinalReport,
    logical_names: frozenset[str],
) -> None:
    expected_names: set[str] = set()
    for result in report.tool_results:
        result_name = f"tool_results/{result.result_id}.json"
        input_name = f"tool_results/{result.result_id}.input.json"
        expected_names.update((result_name, input_name))
        stored_result = parse_canonical_model(_payload(read, result_name), ToolResult)
        stored_input = parse_canonical_json(_payload(read, input_name))
        if stored_result != result or stored_input != result.input:
            raise P3TerminalSourceReadError("P3-030C FinalReport and ToolResult artifacts differ")
    actual_names = {
        name
        for name in logical_names
        if name.startswith("tool_results/") and name.endswith(".json")
    }
    if actual_names != expected_names:
        raise P3TerminalSourceReadError("P3-030C ToolResult artifact inventory is not exact")


def _model_hash(domain: str, value: object) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return canonical_domain_sha256(domain, payload)


def _without_hash(value: BaseModel, field: str) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    payload.pop(field)
    return payload


def _source_hash(domain: str, value: bytes) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\0" + value).hexdigest()


def _provenance_hash(suffix: str, value: object) -> str:
    payload: object
    if isinstance(value, (tuple, list)):
        payload = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value
        ]
    else:
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return canonical_domain_sha256(f"poker-bounded-river-call-ev-{suffix}-v1", payload)


def _verify_candidate_hash_ledger(
    source_bytes: bytes,
    candidate: BoundedRiverCallEvCandidateV1,
) -> None:
    projection = candidate.projection
    bounded = projection.bounded_candidate
    bounded_projection = bounded.projection
    bounded_source = bounded_projection.source
    if (
        bounded_projection.hand.known_ranges
        or bounded_source.bytes_length != len(source_bytes)
        or bounded_source.content_sha256
        != _source_hash(BOUNDED_NL_SOURCE_CANONICALIZATION_ID, source_bytes)
        or bounded_projection.focal_decision.focal_sha256
        != canonical_domain_sha256(
            BOUNDED_NL_FOCAL_CANONICALIZATION_ID,
            _without_hash(bounded_projection.focal_decision, "focal_sha256"),
        )
        or bounded_projection.tool_plan.tool_plan_sha256
        != canonical_domain_sha256(
            BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID,
            _without_hash(bounded_projection.tool_plan, "tool_plan_sha256"),
        )
        or bounded_projection.source_bindings_sha256
        != canonical_domain_sha256(
            BOUNDED_NL_BINDINGS_CANONICALIZATION_ID,
            [item.model_dump(mode="json") for item in bounded_projection.source_bindings],
        )
        or bounded_projection.extractor_sha256
        != canonical_domain_sha256(
            BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID,
            {
                "contract_id": bounded_projection.contract_id,
                "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
                "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
            },
        )
        or bounded.candidate_sha256
        != _model_hash(BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID, bounded_projection)
    ):
        raise P3TerminalSourceReadError("P3-030C bounded source hash ledger differs")

    equity = Fraction(
        projection.equity_model.equity.numerator,
        projection.equity_model.equity.denominator,
    )
    chip_unit = Fraction(
        projection.call_ev_model.chip_unit.numerator,
        projection.call_ev_model.chip_unit.denominator,
    )
    tool_plan_payload = {
        "ordered_tools": BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
        "bounded_tool_plan_sha256": bounded_projection.tool_plan.tool_plan_sha256,
        "range_equity_binding_sha256": projection.range_equity_binding.binding_sha256,
        "range_equity_tool_plan": projection.range_equity_binding.tool_plan,
        "raked_call_ev_input": {
            "equity": float(equity),
            "pot_after_bet": float(projection.call_ev_model.pot_after_bet_units * chip_unit),
            "call_cost": float(projection.call_ev_model.call_cost_units * chip_unit),
            "rake_percent": 0.0,
        },
    }
    expected = {
        "source_sha256": _source_hash(SOURCE_HASH_DOMAIN, source_bytes),
        "bounded_candidate_sha256": _model_hash(BOUNDED_CANDIDATE_HASH_DOMAIN, bounded),
        "source_bindings_sha256": canonical_domain_sha256(
            SOURCE_BINDINGS_HASH_DOMAIN,
            [item.model_dump(mode="json") for item in bounded_projection.source_bindings],
        ),
        "focal_sha256": _model_hash(FOCAL_HASH_DOMAIN, bounded_projection.focal_decision),
        "extractor_sha256": canonical_domain_sha256(
            EXTRACTOR_HASH_DOMAIN,
            {
                "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
                "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
                "bounded_extractor_sha256": bounded_projection.extractor_sha256,
            },
        ),
        "tool_plan_sha256": canonical_domain_sha256(TOOL_PLAN_HASH_DOMAIN, tool_plan_payload),
        "range_definition_sha256": _model_hash(
            RANGE_DEFINITION_HASH_DOMAIN,
            projection.range_definition,
        ),
        "range_target_sha256": _model_hash(RANGE_TARGET_HASH_DOMAIN, projection.range_target),
        "range_binding_sha256": _model_hash(
            RANGE_BINDING_HASH_DOMAIN,
            projection.range_equity_binding,
        ),
        "equity_model_sha256": _model_hash(EQUITY_MODEL_HASH_DOMAIN, projection.equity_model),
        "call_ev_model_sha256": _model_hash(CALL_EV_MODEL_HASH_DOMAIN, projection.call_ev_model),
    }
    if any(getattr(projection, field) != value for field, value in expected.items()) or (
        candidate.candidate_sha256 != _model_hash(CANDIDATE_HASH_DOMAIN, projection)
    ):
        raise P3TerminalSourceReadError("P3-030C candidate hash ledger differs")


def _verify_confirmation_binding_ledger(
    candidate: BoundedRiverCallEvCandidateV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    binding: BoundedRiverCallEvBindingV1,
    provenance: BoundedRiverCallEvProvenanceV1,
) -> None:
    projection = candidate.projection
    fields = (
        "source_sha256",
        "bounded_candidate_sha256",
        "source_bindings_sha256",
        "focal_sha256",
        "extractor_sha256",
        "tool_plan_sha256",
        "range_definition_sha256",
        "range_target_sha256",
        "range_binding_sha256",
        "equity_model_sha256",
        "call_ev_model_sha256",
    )
    if (
        confirmation.intake_id != projection.intake_id
        or confirmation.candidate_sha256 != candidate.candidate_sha256
        or any(getattr(confirmation, field) != getattr(projection, field) for field in fields)
        or confirmation.authority_snapshot_sha256
        != _model_hash(AUTHORITY_HASH_DOMAIN, confirmation.authority)
        or confirmation.confirmation_sha256
        != canonical_domain_sha256(
            CONFIRMATION_HASH_DOMAIN,
            _without_hash(confirmation, "confirmation_sha256"),
        )
        or not confirmation.confirmed_at <= provenance.admitted_at <= confirmation.expires_at
    ):
        raise P3TerminalSourceReadError("P3-030C confirmation hash ledger differs")

    binding_fields = ("run_id", "intake_id", *fields, "candidate_sha256")
    if (
        any(getattr(binding, field) != getattr(confirmation, field) for field in binding_fields)
        or binding.confirmation_sha256 != confirmation.confirmation_sha256
        or binding.ordered_tools != BOUNDED_RIVER_CALL_EV_TOOL_ORDER
    ):
        raise P3TerminalSourceReadError("P3-030C binding and confirmation differ")


def _range_candidate_payload(candidate: BoundedRiverCallEvCandidateV1) -> dict[str, object]:
    projection = candidate.projection
    hand = projection.bounded_candidate.projection.hand.model_dump(mode="json")
    hand["known_ranges"] = [projection.range_definition.model_dump(mode="json")]
    return {
        "case_id": f"range-equity-{projection.intake_id}",
        "kind": "calculation",
        "raw_text": None,
        "hand": hand,
        "focal_decision": None,
        "realized_result": None,
        "analysis_scope": "retrospective",
        "claims": [],
        "evidence": [],
        "assumptions": [],
        "objective": "exact_single_range_river_equity",
        "requested_tools": ["combos", "holdem_equity"],
        "metadata": {},
    }


def _verify_range_and_result_ledger(
    candidate: BoundedRiverCallEvCandidateV1,
    outer_binding: BoundedRiverCallEvBindingV1,
    result: BoundedRiverCallEvResultV1,
    range_equity_binding: VersionedRangeRiverEquityBindingV1,
) -> None:
    projection = candidate.projection
    definition = projection.range_definition
    bounded_hand = projection.bounded_candidate.projection.hand
    binding = range_equity_binding
    oracle = projection.range_equity_oracle
    range_result = result.range_equity_result
    expected_binding = {
        "range_id": definition.range_id,
        "target_player_id": definition.target_player_id,
        "hero_player_id": bounded_hand.hero_player_id,
        "as_of_action_index": definition.game_conditions.as_of_action_index,
        "source_range_sha256": _model_hash(SOURCE_RANGE_HASH_DOMAIN, definition),
        "candidate_sha256": canonical_domain_sha256(
            RANGE_CANDIDATE_HASH_DOMAIN,
            _range_candidate_payload(candidate),
        ),
        "condition_binding_sha256": range_result.condition_binding_sha256,
        "canonical_combo_sha256": range_result.canonical_combo_sha256,
        "oracle_sha256": oracle.oracle_sha256,
        "combo_count": oracle.combo_count,
        "total_weight_millionths": oracle.total_weight_millionths,
    }
    if (
        projection.range_equity_binding != binding
        or binding.binding_sha256
        != canonical_domain_sha256(
            RANGE_HASH_DOMAIN,
            _without_hash(binding, "binding_sha256"),
        )
        or any(getattr(binding, field) != value for field, value in expected_binding.items())
    ):
        raise P3TerminalSourceReadError("P3-030C range binding ledger differs")

    oracle_fields = (
        "range_id",
        "target_player_id",
        "hero_player_id",
        "combo_count",
        "total_weight_millionths",
        "win_combo_count",
        "tie_combo_count",
        "loss_combo_count",
        "win_weight_millionths",
        "tie_weight_millionths",
        "loss_weight_millionths",
        "equity_numerator",
        "equity_denominator",
    )
    if (
        oracle.binding_sha256 != binding.binding_sha256
        or oracle.oracle_sha256 != binding.oracle_sha256
        or any(getattr(oracle, field) != getattr(range_result, field) for field in oracle_fields)
        or range_result.binding_sha256 != binding.binding_sha256
        or range_result.source != definition.source
        or range_result.game_conditions != definition.game_conditions
        or range_result.condition_binding_sha256 != binding.condition_binding_sha256
        or range_result.canonical_combo_sha256 != binding.canonical_combo_sha256
        or range_result.oracle_sha256 != binding.oracle_sha256
        or range_result.hero_cards != tuple(bounded_hand.hero_cards)
        or range_result.board != tuple(bounded_hand.board)
    ):
        raise P3TerminalSourceReadError("P3-030C range result ledger differs")

    equity_model = projection.equity_model
    call_model = projection.call_ev_model
    result_fields = (
        "equity",
        "required_equity",
        "call_ev_units",
        "call_ev_amount",
        "fold_ev_units",
        "call_minus_fold_ev_units",
        "action_comparison",
    )
    if (
        result.binding_sha256 != outer_binding.binding_sha256
        or equity_model.binding_sha256 != binding.binding_sha256
        or equity_model.oracle_sha256 != binding.oracle_sha256
        or equity_model.source_content_status != definition.source.content_status
        or equity_model.equity != result.equity
        or call_model.equity != result.equity
        or any(getattr(call_model, field) != getattr(result, field) for field in result_fields)
        or result.range_source_status != definition.source.content_status
    ):
        raise P3TerminalSourceReadError("P3-030C exact result ledger differs")


def _verify_case_and_report_ledger(
    candidate: BoundedRiverCallEvCandidateV1,
    binding: BoundedRiverCallEvBindingV1,
    range_equity_binding: VersionedRangeRiverEquityBindingV1,
    case: CaseInput,
    report: FinalReport,
    assignments: tuple[AgentAssignment, ...],
    execution_records: tuple[AgentExecutionRecord, ...],
    agent_reports: tuple[AgentReport, ...],
) -> None:
    expected_hand = _range_candidate_payload(candidate)["hand"]
    expected_roles = tuple(item.agent_role for item in assignments)
    projection = candidate.projection
    bounded_projection = projection.bounded_candidate.projection
    call_model = projection.call_ev_model
    chip_unit = Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
    expected_tool_inputs = {
        "hand_pot_ledger": {
            "schema_version": "1.0.0",
            "rule_profile": bounded_projection.tool_plan.ledger_profile.model_dump(mode="json"),
        },
        "pot_odds": bounded_projection.tool_plan.pot_odds_input.model_dump(mode="json"),
        "raked_call_ev": {
            "equity": float(Fraction(call_model.equity.numerator, call_model.equity.denominator)),
            "pot_after_bet": float(call_model.pot_after_bet_units * chip_unit),
            "call_cost": float(call_model.call_cost_units * chip_unit),
            "rake_percent": 0.0,
        },
    }
    focal = bounded_projection.focal_decision
    case_focal = case.focal_decision
    source_status_matches = (
        len(case.claims) == 1
        and case.claims[0].claim_id == f"range-source-{projection.intake_id}"
        and not case.assumptions
        if projection.range_definition.source.content_status == "USER_CLAIM"
        else len(case.assumptions) == 1
        and case.assumptions[0].assumption_id == f"range-source-{projection.intake_id}"
        and not case.claims
    )
    if (
        case.raw_text is not None
        or case.hand is None
        or case.hand.model_dump(mode="json") != expected_hand
        or case.case_id != f"bounded-river-call-ev-{projection.intake_id}"
        or case.kind != "hand"
        or case.analysis_scope != "retrospective"
        or case.objective != "bounded_river_call_or_fold_exact_ev"
        or case.realized_result is not None
        or case.evidence
        or not source_status_matches
        or case_focal is None
        or case_focal.street.value != "river"
        or case_focal.action_index != focal.hero_action_index
        or case_focal.actor != bounded_projection.hand.hero_player_id
        or tuple(case.requested_tools) != BOUNDED_RIVER_CALL_EV_TOOL_ORDER
        or set(case.metadata)
        != {"bounded_river_call_ev", "versioned_range_river_equity", "tool_inputs"}
        or case.metadata.get("bounded_river_call_ev") != binding.model_dump(mode="json")
        or case.metadata.get("versioned_range_river_equity")
        != range_equity_binding.model_dump(mode="json")
        or case.metadata.get("tool_inputs") != expected_tool_inputs
        or report.run_id != binding.run_id
        or report.run_status != "completed"
        or report.reconstructed_input != case.model_dump(mode="json")
        or report.agent_execution_records != list(execution_records)
        or expected_roles != tuple(item.agent_role for item in execution_records)
        or expected_roles != tuple(item.agent_role for item in agent_reports)
        or any(
            record.assignment_id != assignment.assignment_id
            for assignment, record in zip(assignments, execution_records, strict=True)
        )
        or len({item.assignment_id for item in assignments}) != len(assignments)
        or len({item.execution_id for item in execution_records}) != len(execution_records)
        or len({item.report_id for item in agent_reports}) != len(agent_reports)
        or report.analysis_sections
        != [
            {
                "title": item.agent_role,
                "epistemic_status": "UNKNOWN",
                "unverified_conclusions": item.conclusions,
                "unverified_claims": [claim.text for claim in item.claims],
                "uncertainties": item.uncertainties,
                "objections": item.objections,
                "unresolved_questions": item.unresolved_questions,
            }
            for item in agent_reports
        ]
    ):
        raise P3TerminalSourceReadError("P3-030C CaseInput and FinalReport ledgers differ")


def _verify_tool_result_ledger(
    candidate: BoundedRiverCallEvCandidateV1,
    case: CaseInput,
    report: FinalReport,
    result: BoundedRiverCallEvResultV1,
) -> None:
    if (
        tuple(item.tool_name for item in report.tool_results) != BOUNDED_RIVER_CALL_EV_TOOL_ORDER
        or len({item.result_id for item in report.tool_results}) != len(report.tool_results)
        or any(item.status.value != "success" for item in report.tool_results)
        or len(result.tool_support) != len(report.tool_results)
    ):
        raise P3TerminalSourceReadError("P3-030C tool result ledger is incomplete")
    for stored, support in zip(report.tool_results, result.tool_support, strict=True):
        if (
            support.result_id != stored.result_id
            or support.tool_name != stored.tool_name
            or support.status != stored.status.value
            or support.result_sha256 != _model_hash(TOOL_RESULT_HASH_DOMAIN, stored)
        ):
            raise P3TerminalSourceReadError("P3-030C result and ToolResult hashes differ")

    range_results = {item.tool_name: item for item in report.tool_results}
    if case.hand is None:
        raise P3TerminalSourceReadError("P3-030C ToolResult hand authority is missing")
    tool_inputs = case.metadata.get("tool_inputs")
    if not isinstance(tool_inputs, dict):
        raise P3TerminalSourceReadError("P3-030C ToolResult input ledger is missing")
    ledger_input = tool_inputs.get("hand_pot_ledger")
    validation_output = range_results["range_validate"].output
    if (
        not isinstance(ledger_input, dict)
        or range_results["hand_validator"].input != case.hand.model_dump(mode="json")
        or range_results["hand_pot_ledger"].input
        != {**ledger_input, "hand": case.hand.model_dump(mode="json")}
        or range_results["pot_odds"].input != tool_inputs.get("pot_odds")
        or range_results["raked_call_ev"].input != tool_inputs.get("raked_call_ev")
        or range_results["range_validate"].input
        != {
            "schema_version": "1.0.0",
            "hand": case.hand.model_dump(mode="json"),
            "range_definition": candidate.projection.range_definition.model_dump(mode="json"),
        }
        or range_results["combos"].input
        != {"range": validation_output.get("canonical_notation"), "dead_cards": []}
    ):
        raise P3TerminalSourceReadError("P3-030C ToolResult inputs differ from CaseInput")

    range_result = result.range_equity_result
    expected_hashes = {
        "validation_input_sha256": canonical_domain_sha256(
            VALIDATION_INPUT_HASH_DOMAIN,
            range_results["range_validate"].input,
        ),
        "validation_output_sha256": canonical_domain_sha256(
            VALIDATION_OUTPUT_HASH_DOMAIN,
            range_results["range_validate"].output,
        ),
        "combos_input_sha256": canonical_domain_sha256(
            COMBOS_INPUT_HASH_DOMAIN,
            range_results["combos"].input,
        ),
        "combos_output_sha256": canonical_domain_sha256(
            COMBOS_OUTPUT_HASH_DOMAIN,
            range_results["combos"].output,
        ),
        "equity_input_sha256": canonical_domain_sha256(
            EQUITY_INPUT_HASH_DOMAIN,
            range_results["holdem_equity"].input,
        ),
        "equity_output_sha256": canonical_domain_sha256(
            EQUITY_OUTPUT_HASH_DOMAIN,
            range_results["holdem_equity"].output,
        ),
    }
    if any(getattr(range_result, field) != value for field, value in expected_hashes.items()):
        raise P3TerminalSourceReadError("P3-030C range result and ToolResult hashes differ")

    _verify_tool_result_semantics(candidate, result, range_results)


def _verify_tool_contract_metadata(results: dict[str, ToolResult]) -> None:
    for tool_name, result in results.items():
        expected_numeric, expected_qualifier, expected_method = _TOOL_CONTRACT_METADATA[tool_name]
        if (
            result.tool_name != tool_name
            or frozenset(result.output) != _TOOL_OUTPUT_KEYS[tool_name]
            or result.status.value != "success"
            or result.exactness.value != "exact"
            or result.numeric_exactness.value != expected_numeric
            or result.contract_version != "2.0.0"
            or result.version != "1.0.0"
            or result.model_qualifier != expected_qualifier
            or result.method != expected_method
            or result.stochastic is not None
            or result.seed is not None
            or result.samples is not None
            or result.iterations is not None
            or result.confidence_interval is not None
            or result.confidence_level is not None
            or result.error is not None
            or result.reproduce_command
            != (
                f"poker-deliberate calculate {tool_name} --analysis-scope retrospective "
                "--input <input.json>"
            )
        ):
            raise P3TerminalSourceReadError("P3-030C ToolResult contract metadata differs")


def _is_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _snapshot_number(snapshot: dict[str, object], field: str) -> float | None:
    value = snapshot.get(field)
    return float(value) if _is_number(value) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _visible_board(board: list[str], street: str) -> list[str]:
    visible_cards = {"preflop": 0, "flop": 3, "turn": 4, "river": 5, "showdown": 5}
    return board[: visible_cards[street]]


def _valid_hand_validator_nested_shapes(
    validator: HandValidatorOutput,
    candidate: BoundedRiverCallEvCandidateV1,
) -> bool:
    """Validate the untyped nested portions of the persisted validator contract."""

    hand = candidate.projection.bounded_candidate.projection.hand
    player_ids = {player.player_id for player in hand.players}
    reconstructed_keys = {
        "index",
        "pot_after",
        "stacks_after",
        "active_players",
        "all_in_players",
    }
    snapshot_keys = {
        "street",
        "action_index",
        "actor",
        "board",
        "pot_before",
        "to_call",
        "actual_call",
        "contestable_pot",
        "current_bet",
        "actor_invested",
        "stack_behind",
        "history_before",
        "facing_action",
        "side_pot_risk",
    }
    if (
        set(validator.remaining_stacks) != player_ids
        or len(validator.reconstructed_actions) != len(hand.actions)
        or len(validator.decision_snapshots) != len(hand.actions)
    ):
        return False
    for index, reconstructed in enumerate(validator.reconstructed_actions):
        if set(reconstructed) != reconstructed_keys:
            return False
        stacks_after = reconstructed.get("stacks_after")
        active_players = reconstructed.get("active_players")
        all_in_players = reconstructed.get("all_in_players")
        if (
            not _is_int(reconstructed.get("index"))
            or reconstructed.get("index") != index
            or not _is_number(reconstructed.get("pot_after"))
            or float(reconstructed["pot_after"]) < 0
            or not isinstance(stacks_after, dict)
            or set(stacks_after) != player_ids
            or any(not _is_number(value) or float(value) < 0 for value in stacks_after.values())
            or not _is_string_list(active_players)
            or not _is_string_list(all_in_players)
            or active_players != sorted(set(active_players))
            or all_in_players != sorted(set(all_in_players))
            or not set(active_players).issubset(player_ids)
            or not set(all_in_players).issubset(player_ids)
        ):
            return False
    for index, (snapshot, action) in enumerate(
        zip(validator.decision_snapshots, hand.actions, strict=True)
    ):
        actual_call = snapshot.get("actual_call")
        if (
            set(snapshot) != snapshot_keys
            or not _is_int(snapshot.get("action_index"))
            or snapshot.get("action_index") != index
            or snapshot.get("actor") != action.actor
            or snapshot.get("street") != action.street.value
            or snapshot.get("board") != _visible_board(hand.board, action.street.value)
            or any(
                not _is_number(snapshot.get(field))
                for field in (
                    "pot_before",
                    "to_call",
                    "contestable_pot",
                    "current_bet",
                    "actor_invested",
                    "stack_behind",
                )
            )
            or any(
                float(snapshot[field]) < 0
                for field in (
                    "pot_before",
                    "to_call",
                    "contestable_pot",
                    "current_bet",
                    "actor_invested",
                    "stack_behind",
                )
            )
            or (actual_call is not None and not _is_number(actual_call))
            or (actual_call is not None and float(actual_call) < 0)
            or not _is_string_list(snapshot.get("history_before"))
            or not isinstance(snapshot.get("facing_action"), str)
            or not isinstance(snapshot.get("side_pot_risk"), bool)
        ):
            return False
    return True


def _combo_key(first: str, second: str) -> tuple[str, str]:
    return (first, second) if first.encode("ascii") < second.encode("ascii") else (second, first)


def _normalized_combo_weights(
    output: CombosOutput,
) -> dict[tuple[str, str], float] | None:
    if output.normalized_weights is None:
        return None
    weights: dict[tuple[str, str], float] = {}
    for item in output.normalized_weights:
        if set(item) != {"cards", "weight"}:
            return None
        cards = item.get("cards")
        weight = item.get("weight")
        if (
            not isinstance(cards, list)
            or len(cards) != 2
            or any(not isinstance(card, str) for card in cards)
            or not _is_number(weight)
        ):
            return None
        key = _combo_key(cards[0], cards[1])
        if key in weights:
            return None
        weights[key] = float(weight)
    return weights


def _verify_tool_result_semantics(
    candidate: BoundedRiverCallEvCandidateV1,
    result: BoundedRiverCallEvResultV1,
    range_results: dict[str, ToolResult],
) -> None:
    """Cross-bind stored typed outputs without executing any calculator."""

    _verify_tool_contract_metadata(range_results)
    projection = candidate.projection
    bounded = projection.bounded_candidate.projection
    call_model = projection.call_ev_model
    range_result = result.range_equity_result
    chip_unit = Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
    equity = Fraction(result.equity.numerator, result.equity.denominator)
    required_equity = Fraction(
        result.required_equity.numerator,
        result.required_equity.denominator,
    )
    call_ev_amount = Fraction(
        result.call_ev_amount.numerator,
        result.call_ev_amount.denominator,
    )

    validator = HandValidatorOutput.model_validate_json(
        canonical_json_bytes(range_results["hand_validator"].output),
        strict=True,
    )
    focal_snapshot = next(
        (
            item
            for item in validator.decision_snapshots
            if item.get("action_index") == bounded.focal_decision.hero_action_index
        ),
        None,
    )
    if (
        not validator.valid
        or validator.errors
        or not _valid_hand_validator_nested_shapes(validator, candidate)
        or not close_ulps(
            validator.final_pot,
            float(call_model.pot_after_bet_units * chip_unit),
            ulps=32,
        )
        or focal_snapshot is None
        or focal_snapshot.get("actor") != bounded.hand.hero_player_id
        or focal_snapshot.get("street") != "river"
        or focal_snapshot.get("side_pot_risk") is not False
        or focal_snapshot.get("actual_call") is not None
        or _snapshot_number(focal_snapshot, "contestable_pot") is None
        or not close_ulps(
            _snapshot_number(focal_snapshot, "contestable_pot") or 0.0,
            float(call_model.pot_after_bet_units * chip_unit),
            ulps=32,
        )
        or _snapshot_number(focal_snapshot, "to_call") is None
        or not close_ulps(
            _snapshot_number(focal_snapshot, "to_call") or 0.0,
            float(call_model.call_cost_units * chip_unit),
            ulps=32,
        )
    ):
        raise P3TerminalSourceReadError("P3-030C hand validation output differs")

    ledger = HandPotLedgerOutputV1.model_validate_json(
        canonical_json_bytes(range_results["hand_pot_ledger"].output),
        strict=True,
    )
    focal_ledger = ledger.ledger_actions[-1]
    if (
        canonical_domain_sha256(
            f"{BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID}:ledger-input",
            {
                "schema_version": "1.0.0",
                "rule_profile": bounded.tool_plan.ledger_profile.model_dump(mode="json"),
                "hand": bounded.hand.model_dump(mode="json"),
            },
        )
        != bounded.tool_plan.ledger_input_sha256
    ):
        raise P3TerminalSourceReadError("P3-030C integer pot ledger input hash differs")
    if (
        canonical_domain_sha256(
            f"{BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID}:ledger-output",
            range_results["hand_pot_ledger"].output,
        )
        != bounded.tool_plan.ledger_output_sha256
    ):
        raise P3TerminalSourceReadError("P3-030C integer pot ledger output hash differs")
    if (
        Fraction(ledger.chip_unit) != chip_unit
        or ledger.final_pot_units != call_model.pot_before_bet_units
        or ledger.total_returned_units != call_model.opponent_bet_units
        or ledger.gross_committed_units != call_model.pot_after_bet_units
        or len(ledger.uncalled_returns) != 1
        or ledger.uncalled_returns[0].player_id != bounded.focal_decision.selector_actor
        or ledger.uncalled_returns[0].amount_units != call_model.opponent_bet_units
        or focal_ledger.action_index != bounded.focal_decision.hero_action_index
        or focal_ledger.actor != bounded.hand.hero_player_id
        or focal_ledger.action != "fold"
        or focal_ledger.committed_units != 0
        or focal_ledger.pot_units_after != call_model.pot_after_bet_units
    ):
        raise P3TerminalSourceReadError("P3-030C integer pot ledger output differs")

    pot_odds_output = PotOddsOutput.model_validate_json(
        canonical_json_bytes(range_results["pot_odds"].output),
        strict=True,
    )
    pot_after_bet = float(call_model.pot_after_bet_units * chip_unit)
    contestable_pot = float(call_model.contestable_pot_units * chip_unit)
    call_cost = float(call_model.call_cost_units * chip_unit)
    if (
        canonical_domain_sha256(
            f"{BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID}:pot-odds-input",
            range_results["pot_odds"].input,
        )
        != bounded.tool_plan.pot_odds_input_sha256
        or range_results["pot_odds"].input
        != bounded.tool_plan.pot_odds_input.model_dump(mode="json")
        or not close_ulps(pot_odds_output.pot_after_opponent_bet, pot_after_bet, ulps=16)
        or not close_ulps(pot_odds_output.final_pot_before_rake, contestable_pot, ulps=16)
        or not close_ulps(pot_odds_output.expected_rake, 0.0, ulps=16)
        or not close_ulps(pot_odds_output.final_pot_after_rake, contestable_pot, ulps=16)
        or not close_ulps(
            pot_odds_output.required_equity,
            float(required_equity),
            ulps=16,
        )
        or not close_ulps(
            pot_odds_output.required_equity_percent,
            float(required_equity) * 100.0,
            ulps=16,
        )
        or not close_ulps(
            pot_odds_output.pot_odds_against,
            pot_after_bet / call_cost,
            ulps=16,
        )
    ):
        raise P3TerminalSourceReadError("P3-030C pot-odds output differs")

    validation = RangeValidationResultV1.model_validate_json(
        canonical_json_bytes(range_results["range_validate"].output),
        strict=True,
    )
    definition = projection.range_definition
    if (
        validation.status != "success"
        or validation.diagnostics
        or validation.range_id != definition.range_id
        or validation.target_player_id != definition.target_player_id
        or validation.source != definition.source
        or validation.game_conditions != definition.game_conditions
        or validation.source_notation_sha256 != definition.source.content_sha256
        or validation.condition_binding_sha256 != range_result.condition_binding_sha256
        or validation.canonical_combo_sha256 != range_result.canonical_combo_sha256
        or validation.combo_count != range_result.combo_count
        or validation.total_weight_millionths != range_result.total_weight_millionths
        or validation.canonical_notation is None
        or len(validation.combos) != range_result.combo_count
        or sum(item.weight_millionths for item in validation.combos)
        != range_result.total_weight_millionths
    ):
        raise P3TerminalSourceReadError("P3-030C range validation output differs")

    combos = CombosOutput.model_validate_json(
        canonical_json_bytes(range_results["combos"].output),
        strict=True,
    )
    observed_weights = _normalized_combo_weights(combos)
    expected_weights = {
        _combo_key(item.cards[0], item.cards[1]): (
            item.weight_millionths / range_result.total_weight_millionths
        )
        for item in validation.combos
    }
    if (
        combos.range != validation.canonical_notation
        or combos.combo_count != range_result.combo_count
        or combos.total_combo_weight is None
        or not close_ulps(
            combos.total_combo_weight,
            range_result.total_weight_millionths / 1_000_000,
            ulps=32,
        )
        or observed_weights is None
        or set(observed_weights) != set(expected_weights)
        or any(
            not close_ulps(observed_weights[cards], weight, ulps=32)
            for cards, weight in expected_weights.items()
        )
    ):
        raise P3TerminalSourceReadError("P3-030C combo output differs")

    equity_input = range_results["holdem_equity"].input
    expected_equity_input = {
        "hero_range": "".join(bounded.hand.hero_cards),
        "villain_range": validation.canonical_notation,
        "board": list(bounded.hand.board),
        "dead_cards": [],
        "game_type": "NLHE",
        "mode": "exact",
        "max_exact_evaluations": 990,
    }
    equity_output = HoldemEquityOutput.model_validate_json(
        canonical_json_bytes(range_results["holdem_equity"].output),
        strict=True,
    )
    if (
        equity_input != expected_equity_input
        or not equity_output.exact
        or equity_output.method != "exact_enumeration"
        or equity_output.cards_to_come != 0
        or equity_output.range_pair_count != range_result.combo_count
        or equity_output.evaluations != range_result.combo_count
        or equity_output.unweighted_wins != range_result.win_combo_count
        or equity_output.unweighted_ties != range_result.tie_combo_count
        or equity_output.unweighted_losses != range_result.loss_combo_count
        or not close_ulps(equity_output.hero_equity, float(equity), ulps=128)
    ):
        raise P3TerminalSourceReadError("P3-030C exact Hold'em equity output differs")

    call_ev_output = RakedCallEVOutput.model_validate_json(
        canonical_json_bytes(range_results["raked_call_ev"].output),
        strict=True,
    )
    if (
        not close_ulps(call_ev_output.ev, float(call_ev_amount), ulps=32)
        or not close_ulps(call_ev_output.rake_amount, 0.0, ulps=32)
        or not close_ulps(call_ev_output.final_pot_after_rake, contestable_pot, ulps=32)
        or call_ev_output.formula != "equity * (pot_after_bet + call_cost - rake) - call_cost"
        or call_ev_output.model != "single decision, no future betting, declared final-pot rake"
    ):
        raise P3TerminalSourceReadError("P3-030C call-EV output differs")


def _verify_provenance_ledger(
    read: VerifiedRunReadV2,
    *,
    source_revision_root: Path,
    source_bytes: bytes,
    candidate: BoundedRiverCallEvCandidateV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    binding: BoundedRiverCallEvBindingV1,
    result: BoundedRiverCallEvResultV1,
    provenance: BoundedRiverCallEvProvenanceV1,
    range_definition: VersionedRangeDefinitionV1,
    range_equity_binding: VersionedRangeRiverEquityBindingV1,
    case: CaseInput,
    assignments: tuple[AgentAssignment, ...],
    execution_records: tuple[AgentExecutionRecord, ...],
    agent_reports: tuple[AgentReport, ...],
    report: FinalReport,
) -> None:
    expected = {
        "run_id": read.run_id,
        "intake_id": candidate.projection.intake_id,
        "source_sha256": _source_hash(SOURCE_HASH_DOMAIN, source_bytes),
        "candidate_sha256": candidate.candidate_sha256,
        "confirmation_sha256": confirmation.confirmation_sha256,
        "binding_sha256": binding.binding_sha256,
        "range_definition_sha256": _model_hash(
            RANGE_DEFINITION_HASH_DOMAIN,
            range_definition,
        ),
        "range_equity_binding_sha256": range_equity_binding.binding_sha256,
        "result_sha256": result.result_sha256,
        "case_input_sha256": _provenance_hash("case-input-json", case),
        "assignments_sha256": _provenance_hash("assignments-json", assignments),
        "agent_reports_sha256": _provenance_hash("agent-reports-json", agent_reports),
        "execution_records_sha256": _provenance_hash(
            "execution-records-json",
            execution_records,
        ),
        "final_report_sha256": _provenance_hash("final-report-json", report),
        "terminal_revision_root_sha256": canonical_domain_sha256(
            _TERMINAL_ROOT_HASH_DOMAIN,
            str(source_revision_root.resolve(strict=False)).replace("\\", "/"),
        ),
        "terminal_revision": read.revision,
        "terminal_transaction_id": read.transaction_id,
        "terminal_status": "completed",
    }
    if any(getattr(provenance, field) != value for field, value in expected.items()):
        raise P3TerminalSourceReadError("P3-030C provenance hash ledger differs")


def read_verified_p3_terminal_source(
    read: VerifiedRunReadV2,
    *,
    source_revision_root: Path,
) -> VerifiedP3TerminalSourceV1:
    """Replay persisted P3 semantics without invoking a calculator or provider."""

    try:
        if read.read_status is not RunReadStatus.SUCCEEDED or not read.lifecycle_verified:
            raise P3TerminalSourceReadError(
                "P3-030C source must be a verified successful terminal revision"
            )
        logical_names = frozenset(item.inventory.logical_name for item in read.payloads)
        if logical_names & _P3_ARTIFACT_FAMILY != _P3_SUCCESS_ARTIFACTS:
            raise P3TerminalSourceReadError(
                "P3-030C source lacks its exact seven-artifact success family"
            )

        source_bytes = _payload(read, BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT)
        range_definition = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT),
            VersionedRangeDefinitionV1,
        )
        candidate = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT),
            BoundedRiverCallEvCandidateV1,
        )
        confirmation = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT),
            BoundedRiverCallEvConfirmationV1,
        )
        binding = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT),
            BoundedRiverCallEvBindingV1,
        )
        result = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT),
            BoundedRiverCallEvResultV1,
        )
        provenance = parse_canonical_model(
            _payload(read, BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT),
            BoundedRiverCallEvProvenanceV1,
        )
        range_equity_binding = parse_canonical_model(
            _payload(read, RANGE_EQUITY_BINDING_ARTIFACT),
            VersionedRangeRiverEquityBindingV1,
        )
        case = parse_canonical_model(_payload(read, "input.json"), CaseInput)
        report = parse_canonical_model(_payload(read, "final_report.json"), FinalReport)
        assignments = parse_canonical_model_list(
            _payload(read, "assignments.json"),
            AgentAssignment,
        )
        execution_records = parse_canonical_model_list(
            _payload(read, "agent_execution_records.json"),
            AgentExecutionRecord,
        )
        agent_reports = _ordered_agent_reports(read, report, logical_names)
        _verify_tool_artifacts(read, report, logical_names)

        if candidate.projection.range_definition != range_definition or any(
            item.run_id != read.run_id for item in (confirmation, binding, result, provenance)
        ):
            raise P3TerminalSourceReadError(
                "P3-030C typed artifact ledgers do not identify one exact run"
            )

        _verify_candidate_hash_ledger(source_bytes, candidate)
        _verify_confirmation_binding_ledger(candidate, confirmation, binding, provenance)
        _verify_range_and_result_ledger(candidate, binding, result, range_equity_binding)
        _verify_case_and_report_ledger(
            candidate,
            binding,
            range_equity_binding,
            case,
            report,
            assignments,
            execution_records,
            agent_reports,
        )
        _verify_tool_result_ledger(candidate, case, report, result)

        outer_record = read_bounded_river_call_ev_admission_record(
            source_revision_root,
            read.run_id,
            maximum_bytes=_MAX_ADMISSION_RECORD_BYTES,
        )
        range_record = read_range_equity_admission_record(
            source_revision_root,
            read.run_id,
            maximum_bytes=_MAX_ADMISSION_RECORD_BYTES,
        )
        if outer_record is None or range_record is None:
            raise P3TerminalSourceReadError(
                "P3-030C source lacks its immutable pre-execution commitments"
            )
        verify_bounded_river_call_ev_admission_record(outer_record, binding)
        verify_range_equity_admission_record(range_record, range_equity_binding)
        _verify_provenance_ledger(
            read,
            source_revision_root=source_revision_root,
            source_bytes=source_bytes,
            candidate=candidate,
            confirmation=confirmation,
            binding=binding,
            result=result,
            provenance=provenance,
            range_definition=range_definition,
            range_equity_binding=range_equity_binding,
            case=case,
            assignments=assignments,
            execution_records=execution_records,
            agent_reports=agent_reports,
            report=report,
        )
    except P3TerminalSourceReadError:
        raise
    except (CanonicalStorageError, KeyError, TypeError, ValueError) as exc:
        raise P3TerminalSourceReadError("P3-030C calculator-free semantic replay failed") from exc

    return VerifiedP3TerminalSourceV1(
        source_bytes=source_bytes,
        range_definition=range_definition,
        candidate=candidate,
        confirmation=confirmation,
        binding=binding,
        result=result,
        provenance=provenance,
        range_equity_binding=range_equity_binding,
        case=case,
        report=report,
    )


__all__ = [
    "P3TerminalSourceReadError",
    "VerifiedP3TerminalSourceV1",
    "read_verified_p3_terminal_source",
]
