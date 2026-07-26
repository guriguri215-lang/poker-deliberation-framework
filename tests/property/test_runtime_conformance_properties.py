"""Property checks for deterministic conformance hashes and semantic rejection."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.runtime_conformance import (
    ResultV1,
    ToolCapabilityAllowlistV1,
    canonical_json_bytes,
    compare_records,
    parse_conformance_record,
)
from poker_deliberation.schemas import EpistemicLabel
from tests.runtime_conformance_support import NOW, record_pair


@given(
    summary=st.text(
        alphabet=st.characters(
            min_codepoint=0x20,
            max_codepoint=0x7E,
            blacklist_characters="\x7f",
        ),
        min_size=1,
        max_size=80,
    )
)
def test_canonical_record_round_trip_is_deterministic_for_safe_ascii(summary: str) -> None:
    source, _, _, _ = record_pair()
    result = ResultV1(
        result_id=source.result.result_id,
        status=source.result.status,
        summary=summary,
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    record = source.model_copy(update={"result": result})
    encoded = canonical_json_bytes(record)

    assert canonical_json_bytes(parse_conformance_record(encoded)) == encoded


@given(
    capability=st.from_regex(r"[a-z][a-z0-9-]{0,24}", fullmatch=True).filter(
        lambda value: value != "deterministic-calculation"
    )
)
def test_unknown_capabilities_never_conform(capability: str) -> None:
    source, target, codex, python = record_pair()
    allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=(),
        allowed_capabilities=(capability,),
        catalog_status="declared",
        policy_source="fixture",
    )
    changed = target.model_copy(
        update={"assignment": target.assignment.model_copy(update={"allowlist": allowlist})}
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
    assert "unknown-capability" in codes
    assert "allowlist-semantic-mismatch" in codes


@given(classification=st.sampled_from(["public", "sensitive", "restricted"]))
def test_context_classification_changes_never_conform(classification: str) -> None:
    source, target, codex, python = record_pair()
    context = target.assignment.context.model_copy(update={"classification": classification})
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
    assert "context-semantic-mismatch" in codes


@given(tool=st.sampled_from(["hand_validator", "pot_odds", "solver_status"]))
def test_target_tool_allowlist_expansion_never_conforms(tool: str) -> None:
    source, target, codex, python = record_pair()
    allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=(tool,),
        allowed_capabilities=(),
        catalog_status="declared",
        policy_source="fixture",
    )
    changed = target.model_copy(
        update={"assignment": target.assignment.model_copy(update={"allowlist": allowlist})}
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
    assert "allowlist-semantic-mismatch" in codes
