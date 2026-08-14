import copy
import json
from pathlib import Path

import pytest

import poker_deliberation.isolation as isolation_module
import poker_deliberation.orchestrator as orchestrator_module
from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import context_payload
from poker_deliberation.isolation import (
    IsolationError,
    build_blind_decision_context,
    verify_blind_payload,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import DeterministicMockProvider
from poker_deliberation.schemas import (
    AgentReport,
    Assumption,
    CaseInput,
    Claim,
    EpistemicLabel,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)


def _case() -> dict[str, object]:
    return {
        "kind": "hand",
        "hand": {
            "format": "cash",
            "table_size": 2,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {"player_id": "h", "position": "SB", "starting_stack": 100},
                {"player_id": "v", "position": "BB", "starting_stack": 100},
            ],
            "hero_player_id": "h",
            "hero_cards": ["As", "Kh"],
            "board": ["2c", "3d", "4h", "Qd", "5s"],
            "actions": [
                {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
                {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
                {"street": "preflop", "actor": "h", "action": "raise", "amount": 5, "to_amount": 6},
                {"street": "preflop", "actor": "v", "action": "call", "amount": 4},
                {"street": "flop", "actor": "v", "action": "check", "amount": 0},
                {"street": "flop", "actor": "h", "action": "bet", "amount": 10, "to_amount": 10},
                {"street": "flop", "actor": "v", "action": "call", "amount": 10},
                {"street": "turn", "actor": "v", "action": "check", "amount": 0},
                {"street": "turn", "actor": "h", "action": "check", "amount": 0},
            ],
        },
        "focal_decision": {"street": "flop", "action_index": 5, "actor": "h"},
    }


def test_blind_context_fails_closed_when_isolated_hand_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    class RefusingRegistry:
        def execute(
            self,
            name: str,
            payload: dict[str, object],
            *,
            contract_version: str | None = None,
        ) -> ToolResult:
            calls.append((name, payload, contract_version))
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=contract_version or "1.0.0",
                error="hard-isolated validation refused",
            )

    monkeypatch.setattr(isolation_module, "default_registry", RefusingRegistry)
    case = CaseInput.model_validate(_case())

    with pytest.raises(IsolationError, match="hard-isolated hand validation"):
        build_blind_decision_context(case)
    assert len(calls) == 1
    assert calls[0][0] == "hand_validator"
    assert calls[0][1] == case.hand.model_dump(mode="json")  # type: ignore[union-attr]
    assert calls[0][2] == "2.0.0"


def test_blind_payload_is_invariant_to_focal_size_later_actions_and_result() -> None:
    left = _case()
    right = copy.deepcopy(left)
    right_hand = right["hand"]
    assert isinstance(right_hand, dict)
    actions = right_hand["actions"]
    assert isinstance(actions, list)
    actions[5] = {"street": "flop", "actor": "h", "action": "bet", "amount": 20, "to_amount": 20}
    actions[6:] = [{"street": "flop", "actor": "v", "action": "fold", "amount": 0}]
    right["claims"] = [Claim(text="勝ったから正解", label=EpistemicLabel.USER_CLAIM)]
    right["realized_result"] = {
        "raw_text": "villain folded and hero won",
        "winner_player_id": "h",
        "shown_cards": {"v": ["Qc", "Qh"]},
    }
    left_case = CaseInput.model_validate(left)
    right_case = CaseInput.model_validate(right)
    left_context = build_blind_decision_context(left_case)
    right_context = build_blind_decision_context(right_case)
    assert left_context is not None and right_context is not None
    assert verify_blind_payload(left_case, left_context) == verify_blind_payload(
        right_case, right_context
    )
    serialized = verify_blind_payload(right_case, right_context)
    assert "Qd" not in serialized
    assert "5s" not in serialized
    assert '"amount": 20' not in serialized
    assert right_context.focal.stack_behind == 94


def test_short_call_snapshot_excludes_uncalled_excess() -> None:
    data = _case()
    hand = data["hand"]
    assert isinstance(hand, dict)
    hand["board"] = []
    hand["players"] = [
        {"player_id": "h", "position": "SB", "starting_stack": 30},
        {"player_id": "v", "position": "BB", "starting_stack": 100},
    ]
    hand["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "preflop", "actor": "h", "action": "call", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "raise", "amount": 48, "to_amount": 50},
        {"street": "preflop", "actor": "h", "action": "call", "amount": 28},
    ]
    data["focal_decision"] = {"street": "preflop", "action_index": 4, "actor": "h"}
    context = build_blind_decision_context(CaseInput.model_validate(data))
    assert context is not None
    assert context.focal.to_call == 48
    assert context.focal.actual_call == 28
    assert context.focal.contestable_pot == 32
    assert context.focal.side_pot_risk is False


def test_unprovenanced_known_ranges_are_excluded_from_blind_context() -> None:
    data = _case()
    hand = data["hand"]
    assert isinstance(hand, dict)
    hand["known_ranges"] = [
        {
            "player_id": "v",
            "notation": "QQ",
            "source": "showdown-derived",
            "game_conditions": {"future_turn": "Qd"},
        }
    ]

    case = CaseInput.model_validate(data)
    context = build_blind_decision_context(case)

    assert context is not None
    dumped = context.model_dump(mode="json")
    serialized = verify_blind_payload(case, context)
    assert "known_ranges" not in dumped
    assert "showdown-derived" not in serialized
    assert "Qd" not in serialized


def test_strategy_analyst_receives_only_blind_context(tmp_path: Path) -> None:
    scripts = {
        role: AgentReport(agent_role=role, task=ROLE_CATALOG[role].purpose)
        for role in ("intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator")
    }
    provider = DeterministicMockProvider(scripts)
    data = _case()
    data["raw_text"] = "希望: call; 結果: won"
    data["analysis_scope"] = "retrospective"
    data["objective"] = "prove the winning action was right"
    data["assumptions"] = [Assumption(text="winning proves correctness", reason="user preference")]
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider)
    report = orchestrator.run(CaseInput.model_validate(data))
    contexts = {role: context for role, context in provider.contexts}
    blind = contexts["strategy-analyst"]
    assert blind.objective == "decision_quality_baseline"
    assert blind.blind_decision_context is not None
    assert blind.raw_text is None
    assert blind.hand is None
    assert blind.claims == []
    assert blind.assumptions == []
    verified = orchestrator.product_store.read_current(report.run_id)
    assignments = json.loads(verified.payload_bytes("assignments.json"))
    strategy_assignment = next(
        item for item in assignments if item["agent_role"] == "strategy-analyst"
    )
    assert strategy_assignment["context_keys"] == sorted(context_payload(blind))
    assert "raw_text" not in strategy_assignment["context_keys"]
    assert "hand" not in strategy_assignment["context_keys"]


def test_claim_mentioning_visible_long_player_id_does_not_trigger_isolation_error(
    tmp_path: Path,
) -> None:
    scripts = {
        role: AgentReport(agent_role=role, task=ROLE_CATALOG[role].purpose)
        for role in ("intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator")
    }
    provider = DeterministicMockProvider(scripts)
    data = _case()
    hand = data["hand"]
    assert isinstance(hand, dict)
    players = hand["players"]
    actions = hand["actions"]
    assert isinstance(players, list) and isinstance(actions, list)
    players[0]["player_id"] = "hero_longname"
    hand["hero_player_id"] = "hero_longname"
    for action in actions:
        if action["actor"] == "h":
            action["actor"] = "hero_longname"
    data["focal_decision"] = {
        "street": "flop",
        "action_index": 5,
        "actor": "hero_longname",
    }
    data["claims"] = [
        Claim(
            text="hero_longname のコールは正しかった",
            label=EpistemicLabel.USER_CLAIM,
        )
    ]
    data["analysis_scope"] = "retrospective"

    report = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider).run(
        CaseInput.model_validate(data)
    )

    assert report.run_status == "completed"
    assert any(role == "strategy-analyst" for role, _ in provider.contexts)


def test_isolation_error_becomes_structured_failed_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = {
        role: AgentReport(agent_role=role, task=ROLE_CATALOG[role].purpose)
        for role in ("intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator")
    }
    provider = DeterministicMockProvider(scripts)

    def fail_isolation(_case: CaseInput) -> None:
        raise IsolationError("forced isolation failure")

    monkeypatch.setattr(
        orchestrator_module,
        "build_blind_decision_context",
        fail_isolation,
    )
    data = _case()
    data["analysis_scope"] = "retrospective"
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider)

    report = orchestrator.run(CaseInput.model_validate(data))
    assert report.run_status == "failed_with_limitations"
    assert any("blind decision isolation failed" in item for item in report.data_quality)
    assert [role for role, _ in provider.contexts] == ["intake"]
    verified = orchestrator.product_store.read_current(report.run_id)
    assert json.loads(verified.payload_bytes("state.json"))["state"] == ("FAILED_WITH_LIMITATIONS")
    assert verified.payload_bytes("final_report.json")
