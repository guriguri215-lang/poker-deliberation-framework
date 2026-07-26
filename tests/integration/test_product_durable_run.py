"""Integration tests for P2-012B product durable-run wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def test_ordinary_run_is_published_as_verified_terminal_v2(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)

    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="review",
            analysis_scope="retrospective",
        ),
        run_id="run-product-success",
    )
    verified = orchestrator.product_store.read_current(report.run_id)

    assert report.run_status == "completed"
    assert verified.read_status is RunReadStatus.SUCCEEDED
    assert verified.reachable_revisions == (1,)
    assert verified.budget_settlement_verified is True
    assert verified.lifecycle_verified is True
    assert not (config.runs_dir / report.run_id).exists()
    assert (
        config.revision_runs_dir / "runs" / report.run_id / ".terminal-store" / "current.json"
    ).is_file()
    assert (
        config.durable_budget_runs_dir / "runs" / report.run_id / ".revision-store" / "current.json"
    ).is_file()

    reader = Orchestrator(config)
    assert reader.load_report(report.run_id) == report
    assert reader.report_path(report.run_id, "json").read_bytes() == (
        verified.payload_bytes("final_report.json")
    )


def test_approval_checkpoint_resumes_to_verified_terminal_revision(
    tmp_path: Path,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    report = orchestrator.run(
        CaseInput(
            kind="strategy",
            raw_text="external solver",
            analysis_scope="retrospective",
            metadata={
                "approval_requests": [
                    {
                        "approval_id": "approval-product",
                        "requested_action": "external solver",
                        "reason": "test",
                        "expected_benefit": "solver output",
                        "risks": ["external execution"],
                        "cost_or_resource_estimate": "unknown",
                        "alternatives": ["local sensitivity"],
                        "effect_of_declining": "no solver result",
                    }
                ]
            },
        ),
        run_id="run-product-approval",
    )
    checkpoint = orchestrator.product_store.read_current(report.run_id)

    assert report.run_status == "approval_required"
    assert checkpoint.read_status is RunReadStatus.APPROVAL_REQUIRED
    assert checkpoint.resume_eligible is True
    assert checkpoint.completion_marker is None

    resumed = Orchestrator(_config(tmp_path)).resume(
        report.run_id,
        reject_ids=["approval-product"],
    )
    terminal = orchestrator.product_store.read_current(report.run_id)

    assert resumed.run_status == "completed"
    assert terminal.read_status is RunReadStatus.SUCCEEDED
    assert terminal.reachable_revisions == (2, 1)
    assert terminal.resume_eligible is False


def test_product_artifacts_are_canonical_and_flat_root_stays_read_only(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    legacy = config.runs_dir / "legacy-existing"
    legacy.mkdir(parents=True)
    sentinel = legacy / ".poker-deliberation-run"
    sentinel.write_bytes(b"v1\n")
    source = legacy / "final_report.json"
    source.write_text(json.dumps({"legacy": True}), encoding="utf-8")
    before = {
        path.relative_to(legacy).as_posix(): path.read_bytes()
        for path in legacy.rglob("*")
        if path.is_file()
    }

    report = Orchestrator(config).run(
        CaseInput(
            kind="calculation",
            raw_text="review",
            analysis_scope="retrospective",
        ),
        run_id="run-product-other",
    )
    after = {
        path.relative_to(legacy).as_posix(): path.read_bytes()
        for path in legacy.rglob("*")
        if path.is_file()
    }
    current = Orchestrator(config).product_store.read_current(report.run_id)

    assert before == after
    for payload in current.payloads:
        if payload.inventory.serialization == "poker-run-storage-json-v1":
            assert not payload.exact_bytes.endswith(b"\n")


def test_initialized_product_roots_are_reused_without_reinitialization(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = Orchestrator(config).run(
        CaseInput(
            kind="calculation",
            raw_text="first",
            analysis_scope="retrospective",
        ),
        run_id="run-product-first",
    )
    product_ownership = (config.revision_runs_dir / "ownership.json").read_bytes()
    budget_ownership = (config.durable_budget_runs_dir / "ownership.json").read_bytes()

    second = Orchestrator(config).run(
        CaseInput(
            kind="calculation",
            raw_text="second",
            analysis_scope="retrospective",
        ),
        run_id="run-product-second",
    )

    assert first.run_status == "completed"
    assert second.run_status == "completed"
    assert (config.revision_runs_dir / "ownership.json").read_bytes() == product_ownership
    assert (config.durable_budget_runs_dir / "ownership.json").read_bytes() == budget_ownership


def test_invalid_run_id_is_refused_before_product_root_initialization(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)

    with pytest.raises(ProductRunError) as caught:
        orchestrator.run(
            CaseInput(
                kind="calculation",
                raw_text="invalid id",
                analysis_scope="retrospective",
            ),
            run_id="../escape",
        )

    assert caught.value.failure.code is ProductRunFailureCode.PATH_CONFINEMENT_FAILED
    assert not config.revision_runs_dir.exists()
    assert not config.durable_budget_runs_dir.exists()


def test_absolute_legacy_defaults_resolve_to_distinct_sibling_roots(
    tmp_path: Path,
) -> None:
    first = AppConfig(runs_dir=tmp_path / "first").resolved_storage_roots()
    second = AppConfig(runs_dir=tmp_path / "second").resolved_storage_roots()

    assert len(set(first)) == 3
    assert len(set(second)) == 3
    assert first[1:] != second[1:]
    for roots in (first, second):
        AppConfig._validate_nonoverlapping_roots(roots)


def test_explicit_overlapping_product_roots_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pairwise nonoverlapping"):
        Orchestrator(
            AppConfig(
                runs_dir=tmp_path / "legacy",
                revision_runs_dir=tmp_path / "legacy" / "product",
                durable_budget_runs_dir=tmp_path / "budget",
            )
        )


def test_tool_result_metadata_and_reproduction_input_round_trip_exactly(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="approximate tool metadata",
            analysis_scope="retrospective",
            requested_tools=["holdem_equity"],
            metadata={
                "tool_inputs": {
                    "holdem_equity": {
                        "hero_range": "AsAh",
                        "villain_range": "KcKd",
                        "mode": "monte_carlo",
                        "samples": 1,
                        "seed": 7,
                    }
                }
            },
        ),
        run_id="run-product-tool-roundtrip",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    loaded = Orchestrator(config).load_report(report.run_id)
    result = report.tool_results[0]
    input_name = f"tool_results/{result.result_id}.input.json"
    result_name = f"tool_results/{result.result_id}.json"
    argv = json.loads(report.reproduction_steps[0].removeprefix("argv-json: "))

    assert loaded == report
    assert verified.payload_bytes(result_name) == canonical_json_bytes(result)
    assert verified.payload_bytes(input_name) == canonical_json_bytes(result.input)
    assert Path(argv[-1]).read_bytes() == verified.payload_bytes(input_name)
