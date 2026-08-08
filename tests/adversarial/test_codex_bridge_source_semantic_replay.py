from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import poker_deliberation.bounded_natural_language as bounded_natural_language
import poker_deliberation.bounded_river_call_ev as bounded_river_call_ev
import poker_deliberation.bounded_river_call_ev_provenance as bounded_river_provenance
import poker_deliberation.range_equity as range_equity
import poker_deliberation.range_grammar as range_grammar
from poker_deliberation.codex_bridge.source import (
    BridgeSourceError,
    project_verified_p3_terminal,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.storage.revision_canonical import canonical_json_bytes, sha256_bytes
from poker_deliberation.storage.terminal_canonical import (
    completion_marker_sha256,
    current_pointer_sha256,
    manifest_sha256,
    required_inventory_sha256,
    terminal_inventory_sha256,
)
from poker_deliberation.storage.terminal_models import VerifiedPayloadV2, VerifiedRunReadV2
from poker_deliberation.tools.registry import ToolRegistry
from tests.bounded_river_call_ev_support import admission, app_config


def _completed_read(tmp_path: Path) -> tuple[Orchestrator, VerifiedRunReadV2]:
    orchestrator = Orchestrator(config=app_config(tmp_path), provider=LocalProvider())
    report = orchestrator.run_bounded_river_call_ev_review(
        admission(run_id="run-codex-source-semantic-replay")
    )
    return orchestrator, orchestrator.product_store.read_current(report.run_id)


def _rehash_generic_terminal(
    read: VerifiedRunReadV2,
    changes: dict[str, bytes],
) -> VerifiedRunReadV2:
    """Rebuild only the generic terminal hashes around deliberately changed payloads."""

    payloads: list[VerifiedPayloadV2] = []
    for payload in read.payloads:
        exact_bytes = changes.get(payload.inventory.logical_name, payload.exact_bytes)
        inventory = payload.inventory.model_copy(
            update={
                "size_bytes": len(exact_bytes),
                "sha256": sha256_bytes(exact_bytes),
            }
        )
        payloads.append(VerifiedPayloadV2(inventory=inventory, exact_bytes=exact_bytes))
    entries = tuple(item.inventory for item in payloads)
    inventory_sha = terminal_inventory_sha256(entries)
    manifest = read.manifest.model_copy(
        update={"inventory_sha256": inventory_sha, "artifacts": entries}
    )
    manifest_sha = manifest_sha256(manifest)
    assert read.completion_marker is not None
    marker = read.completion_marker.model_copy(
        update={
            "terminal_manifest_sha256": manifest_sha,
            "required_inventory_sha256": required_inventory_sha256(entries),
        }
    )
    marker_sha = completion_marker_sha256(marker)
    pointer = read.pointer.model_copy(
        update={
            "manifest_sha256": manifest_sha,
            "inventory_sha256": inventory_sha,
            "completion_marker_sha256": marker_sha,
        }
    )
    return VerifiedRunReadV2(
        read_status=read.read_status,
        run_id=read.run_id,
        revision=read.revision,
        transaction_id=read.transaction_id,
        current_pointer_sha256=current_pointer_sha256(pointer),
        manifest_sha256=manifest_sha,
        inventory_sha256=inventory_sha,
        completion_marker_sha256=marker_sha,
        resume_eligible=read.resume_eligible,
        budget_settlement_verified=True,
        lifecycle_verified=True,
        reachable_revisions=read.reachable_revisions,
        pointer=pointer,
        manifest=manifest,
        completion_marker=marker,
        payloads=tuple(payloads),
    )


def _source_mutation(read: VerifiedRunReadV2) -> dict[str, bytes]:
    name = "bounded_river_call_ev_source.txt"
    return {name: read.payload_bytes(name) + b" "}


def _range_mutation(read: VerifiedRunReadV2) -> dict[str, bytes]:
    name = "bounded_river_call_ev_range.json"
    value = json.loads(read.payload_bytes(name))
    value["notation"] = "KK"
    return {name: canonical_json_bytes(value)}


def _confirmation_mutation(read: VerifiedRunReadV2) -> dict[str, bytes]:
    name = "bounded_river_call_ev_confirmation.json"
    value = json.loads(read.payload_bytes(name))
    value["authority"]["authority_id"] = "forged-authority"
    return {name: canonical_json_bytes(value)}


def _tool_result_mutation(read: VerifiedRunReadV2) -> dict[str, bytes]:
    report_name = "final_report.json"
    report = json.loads(read.payload_bytes(report_name))
    changed = report["tool_results"][0]
    changed["duration_seconds"] = changed["duration_seconds"] + 0.25
    result_name = f"tool_results/{changed['result_id']}.json"
    return {
        report_name: canonical_json_bytes(report),
        result_name: canonical_json_bytes(changed),
    }


@pytest.mark.parametrize(
    "mutate",
    (_source_mutation, _range_mutation, _confirmation_mutation, _tool_result_mutation),
    ids=("source", "range", "confirmation", "tool-result"),
)
def test_bridge_rejects_semantic_mutation_after_generic_terminal_rehash(
    tmp_path: Path,
    mutate: Callable[[VerifiedRunReadV2], dict[str, bytes]],
) -> None:
    orchestrator, read = _completed_read(tmp_path)
    forged = _rehash_generic_terminal(read, mutate(read))

    with pytest.raises(BridgeSourceError, match="semantic replay"):
        project_verified_p3_terminal(
            forged,
            source_revision_root=orchestrator.product_store.revision_root,
        )


def test_bridge_semantic_replay_does_not_execute_calculators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, read = _completed_read(tmp_path)

    def forbidden_execute(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("terminal semantic replay executed a calculator")

    monkeypatch.setattr(ToolRegistry, "execute", forbidden_execute)
    for module, names in (
        (
            bounded_natural_language,
            ("verify_bounded_candidate", "verify_bounded_source_candidate"),
        ),
        (
            bounded_river_call_ev,
            (
                "calculate_hand_pot_ledger",
                "validate_hand",
                "pot_odds",
                "raked_call_ev",
                "admit_versioned_range_river_equity",
                "build_versioned_range_river_equity_result",
                "exact_versioned_range_river_equity_oracle",
                "verify_versioned_range_river_equity_tool_chain",
                "build_bounded_river_call_ev_result",
                "verify_bounded_candidate",
                "verify_bounded_source_candidate",
                "verify_bounded_river_call_ev_candidate",
                "verify_bounded_river_call_ev_tool_chain",
                "_admit_at",
                "_candidate_from_components",
                "_direct_tool_oracles",
                "_exact_models",
                "_range_equity_candidate",
            ),
        ),
        (
            bounded_river_provenance,
            (
                "build_bounded_river_call_ev_provenance",
                "verify_bounded_river_call_ev_provenance",
                "verify_bounded_river_call_ev_structural_provenance",
                "verify_bounded_river_call_ev_tool_chain",
                "_admit_at",
            ),
        ),
        (
            range_equity,
            (
                "admit_versioned_range_river_equity",
                "build_versioned_range_river_equity_result",
                "exact_versioned_range_river_equity_oracle",
                "validate_versioned_range",
                "verify_versioned_range_river_equity_tool_chain",
                "evaluate_holdem",
            ),
        ),
        (range_grammar, ("validate_versioned_range",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden_execute)

    source = project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )

    assert source.source.source_terminal_run_id == read.run_id
