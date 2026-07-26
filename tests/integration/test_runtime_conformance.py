"""P2-025A projection over ordinary verified offline product runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.runtime_conformance import (
    ProductProjectionError,
    ResultStatus,
    build_runtime_inventories,
    project_python_product_run,
    validate_record,
)
from poker_deliberation.schemas import CanonicalHand, CaseInput, EpistemicLabel
from poker_deliberation.tools import default_registry
from tests.runtime_conformance_support import HASH_A, ROOT


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _case(name: str) -> CaseInput:
    raw = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
    return CaseInput.model_validate(raw)


def _python_inventory() -> object:
    descriptions = default_registry().describe()
    tools = tuple(
        sorted(
            (str(item["name"]) for item in descriptions),
            key=lambda item: item.encode("utf-8"),
        )
    )
    capabilities = tuple(
        sorted(
            (item.capability_id for item in CAPABILITIES),
            key=lambda item: item.encode("utf-8"),
        )
    )
    _, python = build_runtime_inventories(
        ROOT,
        source_revision=HASH_A,
        python_tool_catalog=tools,
        python_capability_catalog=capabilities,
    )
    return python


def test_verified_offline_products_project_without_a_runtime_bridge(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    correction = orchestrator.run(
        _case("wrong_pot_odds_case.json"),
        run_id="p2-025a-pot-odds",
    )
    hand_payload = json.loads((ROOT / "examples" / "valid_hand.json").read_text(encoding="utf-8"))
    hand = orchestrator.run(
        CaseInput(
            kind="hand",
            hand=CanonicalHand.model_validate(hand_payload),
            analysis_scope="retrospective",
        ),
        run_id="p2-025a-hand-validation",
    )
    limitation = orchestrator.run(
        _case("full_nlhe_limitations_case.json"),
        run_id="p2-025a-solver-limitation",
    )
    inventory = _python_inventory()

    projected = []
    for report in (correction, hand, limitation):
        verified = orchestrator.product_store.read_current(report.run_id)
        record = project_python_product_run(
            report,
            verified,
            inventory,  # type: ignore[arg-type]
        )
        projected.append(record)
        assert (
            validate_record(
                record,
                inventory,  # type: ignore[arg-type]
                now=verified.completion_marker.published_at,  # type: ignore[union-attr]
            ).status
            == "conformant"
        )
        assert record.runtime_bridge_used is False
        assert record.execution_audit is not None
        assert record.execution_audit.execution_kind == "python-product-run"
        assert record.execution_audit.manifest_sha256 == verified.manifest_sha256
        assert record.execution_audit.inventory_sha256 == verified.inventory_sha256

    assert projected[0].result.status is ResultStatus.SUCCEEDED
    assert projected[0].result.epistemic_label is EpistemicLabel.CALCULATED
    assert projected[1].result.status is ResultStatus.SUCCEEDED
    assert projected[1].result.epistemic_label is EpistemicLabel.CALCULATED
    assert projected[2].result.status is ResultStatus.LIMITED
    assert projected[2].result.strategy_claim == "none"
    assert projected[2].result.solver_evidence is None
    assert projected[2].result.epistemic_label is EpistemicLabel.CALCULATED
    assert "tool-unavailable:solver_status" in projected[2].result.limitations
    solver_reference = next(
        item for item in projected[2].result.tool_results if item.tool_name == "solver_status"
    )
    assert solver_reference.status == "unavailable"
    assert solver_reference.exactness == "unavailable"


def test_projection_rejects_report_bytes_not_in_verified_product(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    report = orchestrator.run(
        _case("wrong_pot_odds_case.json"),
        run_id="p2-025a-tamper-rejection",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    changed = report.model_copy(update={"conclusion": "Changed after publication."})

    with pytest.raises(ProductProjectionError, match="verified product bytes"):
        project_python_product_run(
            changed,
            verified,
            _python_inventory(),  # type: ignore[arg-type]
        )
