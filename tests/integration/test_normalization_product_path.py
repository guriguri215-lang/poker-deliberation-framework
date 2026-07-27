from __future__ import annotations

import json
from pathlib import Path

import pytest

from poker_deliberation.cli import main
from poker_deliberation.config import AppConfig
from poker_deliberation.normalization import (
    NORMALIZATION_METADATA_KEY,
    NormalizationResultV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import ToolStatus
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.storage.terminal_canonical import (
    UnsupportedTerminalVersion,
    validate_payload_bytes,
)
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)

ROOT = Path(__file__).resolve().parents[2]


def _set_run_roots(monkeypatch, tmp_path: Path) -> AppConfig:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("POKER_DELIBERATION_REVISION_RUNS_DIR", str(tmp_path / "product"))
    monkeypatch.setenv("POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR", str(tmp_path / "budget"))
    return AppConfig.from_env()


def test_cli_product_reader_and_hand_validator_share_one_typed_normalization_record(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    config = _set_run_roots(monkeypatch, tmp_path)

    assert (
        main(
            [
                "review-hand",
                "--file",
                str(ROOT / "examples" / "free_text_hand.txt"),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    read = Orchestrator(config).product_store.read_current(report["run_id"])

    assert read.read_status is RunReadStatus.SUCCEEDED
    names = {payload.inventory.logical_name for payload in read.payloads}
    assert "normalization.json" in names
    record = parse_canonical_model(
        read.payload_bytes("normalization.json"),
        NormalizationResultV1,
    )
    normalized = json.loads(read.payload_bytes("normalized_case.json"))
    source = (ROOT / "examples" / "free_text_hand.txt").read_bytes()
    assert record.status == "success"
    assert record.provenance.source_bytes_length == len(source)
    assert record.hand is not None
    assert normalized["hand"] == record.hand.model_dump(mode="json")
    assert (
        NORMALIZATION_METADATA_KEY not in json.loads(read.payload_bytes("input.json"))["metadata"]
    )
    validator = next(
        item for item in report["tool_results"] if item["tool_name"] == "hand_validator"
    )
    assert validator["status"] == ToolStatus.SUCCESS.value
    assert validator["output"]["valid"] is True
    future = record.model_dump(mode="json")
    future["result_version"] = "9.0.0"
    normalization_inventory = next(
        payload.inventory
        for payload in read.payloads
        if payload.inventory.logical_name == "normalization.json"
    )
    with pytest.raises(UnsupportedTerminalVersion):
        validate_payload_bytes(normalization_inventory, canonical_json_bytes(future))


def test_structured_json_hand_does_not_invent_normalization_provenance(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    payload = json.loads((ROOT / "examples" / "valid_hand.json").read_text(encoding="utf-8"))
    from poker_deliberation.schemas import CanonicalHand, CaseInput

    report = Orchestrator(config).run(
        CaseInput(
            kind="hand",
            hand=CanonicalHand.model_validate(payload),
            analysis_scope="retrospective",
        ),
        run_id="structured-no-normalization",
    )
    read = Orchestrator(config).product_store.read_current(report.run_id)

    assert "normalization.json" not in {payload.inventory.logical_name for payload in read.payloads}


def test_product_reader_fails_closed_after_normalization_payload_tamper(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    config = _set_run_roots(monkeypatch, tmp_path)
    assert (
        main(
            [
                "review-hand",
                "--file",
                str(ROOT / "examples" / "free_text_hand.txt"),
                "--format",
                "json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    store = Orchestrator(config).product_store
    read = store.read_current(report["run_id"])
    path = store.planned_payload_path(
        read.run_id,
        revision=read.revision,
        transaction_id=read.transaction_id,
        logical_name="normalization.json",
    )
    original = path.read_bytes()
    path.write_bytes(original[:-1] + (b" " if original[-1:] != b" " else b"\t"))

    with pytest.raises(ProductRunError) as caught:
        store.read_current(read.run_id)

    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert caught.value.failure.read_status is RunReadStatus.CORRUPT
