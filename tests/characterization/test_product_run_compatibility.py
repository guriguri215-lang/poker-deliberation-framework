"""Compatibility tests for P2-012B product durable-run wiring."""

from __future__ import annotations

import inspect
from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.run_store import RunStore


def test_public_orchestrator_signatures_preserve_existing_parameters() -> None:
    constructor = inspect.signature(Orchestrator)
    run = inspect.signature(Orchestrator.run)
    resume = inspect.signature(Orchestrator.resume)
    load_report = inspect.signature(Orchestrator.load_report)
    report_path = inspect.signature(Orchestrator.report_path)

    assert tuple(constructor.parameters)[:4] == (
        "config",
        "registry",
        "provider",
        "context_clock",
    )
    assert constructor.parameters["terminal_clock"].kind is inspect.Parameter.KEYWORD_ONLY
    assert constructor.parameters["terminal_id_factory"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tuple(run.parameters) == ("self", "case", "run_id")
    assert run.parameters["run_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tuple(resume.parameters) == (
        "self",
        "run_id",
        "approve_ids",
        "reject_ids",
        "reason",
    )
    assert tuple(load_report.parameters) == ("self", "run_id")
    assert tuple(report_path.parameters) == ("self", "run_id", "format_name")


def test_flat_v1_writer_uses_the_exact_portable_lf_sentinel(tmp_path: Path) -> None:
    run = RunStore(tmp_path / "legacy").create_run("run-v1-sentinel")

    assert (run / ".poker-deliberation-run").read_bytes() == b"v1\n"


def test_ordinary_explicit_product_run_does_not_create_flat_v1_run(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    report = Orchestrator(config).run(
        CaseInput(
            kind="calculation",
            raw_text="compatibility fixture",
            analysis_scope="retrospective",
        ),
        run_id="run-product-only",
    )

    assert report.run_status == "completed"
    assert not (config.runs_dir / report.run_id).exists()
