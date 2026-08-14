"""Integration tests for P2-012B product durable-run wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from poker_deliberation.budgets import BudgetPolicyV2, canonical_json_utf8_size
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases.contracts import successful_outcome
from poker_deliberation.schemas import (
    CaseInput,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
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


def test_load_report_rejects_a_hash_valid_but_calculator_invalid_tool_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="combination replay",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-tool-replay-tamper",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    result = next(item for item in report.tool_results if item.tool_name == "combos")
    forged_result = result.model_copy(
        update={
            "output": {
                **result.output,
                "count": int(result.output["count"]) + 1,
            }
        }
    )
    forged_report = report.model_copy(
        update={
            "tool_results": [
                forged_result if item.result_id == result.result_id else item
                for item in report.tool_results
            ]
        }
    )

    class ForgedVerifiedRead:
        read_status = verified.read_status

        def payload_bytes(self, logical_name: str) -> bytes:
            if logical_name == "final_report.json":
                return canonical_json_bytes(forged_report)
            return verified.payload_bytes(logical_name)

    monkeypatch.setattr(
        orchestrator.product_store,
        "read_current",
        lambda _run_id: ForgedVerifiedRead(),
    )

    with pytest.raises(ProductRunError) as caught:
        orchestrator.load_report(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert caught.value.failure.stage == "load_report_tool_verification"


def test_load_report_rejects_general_forged_budget_failure_without_external_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="combination replay",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-forged-budget-failure",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    actual = next(item for item in report.tool_results if item.tool_name == "combos")
    forged = ToolResult(
        result_id=actual.result_id,
        tool_name=actual.tool_name,
        input=actual.input,
        status=ToolStatus.FAILED,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        contract_version=actual.contract_version,
        error="strict budget failure: tool_input_exceeded",
    )
    forged_report = report.model_copy(
        update={
            "tool_results": [
                forged if item.result_id == actual.result_id else item
                for item in report.tool_results
            ]
        }
    )

    class ForgedVerifiedRead:
        read_status = verified.read_status

        def payload_bytes(self, logical_name: str) -> bytes:
            if logical_name == "final_report.json":
                return canonical_json_bytes(forged_report)
            return verified.payload_bytes(logical_name)

    monkeypatch.setattr(
        orchestrator.product_store,
        "read_current",
        lambda _run_id: ForgedVerifiedRead(),
    )

    with pytest.raises(ProductRunError) as caught:
        orchestrator.load_report(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert caught.value.failure.stage == "load_report_tool_verification"


def test_general_budget_failure_remains_ephemeral_without_external_authority(
    tmp_path: Path,
) -> None:
    orchestrator = Orchestrator(
        _config(tmp_path),
        budget_policy=BudgetPolicyV2(max_tool_input_bytes=1_024),
    )
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="oversized general tool input",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={
                "tool_inputs": {
                    "combos": {
                        "range": ",".join(["AA"] * 500),
                        "dead_cards": [],
                    }
                }
            },
        ),
        run_id="run-product-general-budget-ephemeral",
    )

    assert report.run_status == "failed_with_limitations"
    assert report.tool_results[-1].error == "strict budget failure: tool_input_exceeded"
    assert "product persistence refused: tool result lacks independent replay authority" in (
        report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_injected_tool_executor_success_cannot_issue_publication_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    original_run = orchestrator.tool_research_executor.run

    def forged_run(request):  # type: ignore[no-untyped-def]
        outcome = original_run(request)
        assert outcome.output is not None
        binding = outcome.output.bindings[0]
        forged_result = binding.result.model_copy(
            update={
                "output": {
                    **binding.result.output,
                    "count": int(binding.result.output["count"]) + 1,
                }
            }
        )
        forged_output = outcome.output.model_copy(
            update={"bindings": (binding.model_copy(update={"result": forged_result}),)}
        )
        return successful_outcome(request, forged_output)

    monkeypatch.setattr(orchestrator.tool_research_executor, "run", forged_run)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="forged executor result",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-forged-executor-success",
    )

    assert report.run_status == "failed_with_limitations"
    assert "product persistence refused: tool result lacks independent replay authority" in (
        report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_self_removing_tool_executor_shadow_cannot_issue_publication_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    executor = orchestrator.tool_research_executor
    original_run = executor.run

    def self_removing_forged_run(request):  # type: ignore[no-untyped-def]
        monkeypatch.delattr(executor, "run")
        outcome = original_run(request)
        assert outcome.output is not None
        binding = outcome.output.bindings[0]
        forged_result = binding.result.model_copy(
            update={
                "output": {
                    **binding.result.output,
                    "count": int(binding.result.output["count"]) + 1,
                }
            }
        )
        forged_output = outcome.output.model_copy(
            update={"bindings": (binding.model_copy(update={"result": forged_result}),)}
        )
        return successful_outcome(request, forged_output)

    monkeypatch.setattr(executor, "run", self_removing_forged_run)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="self-removing forged executor result",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-self-removing-forged-executor-success",
    )

    assert "run" not in vars(executor)
    assert report.run_status == "failed_with_limitations"
    assert "product persistence refused: tool result lacks independent replay authority" in (
        report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_self_removing_registry_dispatch_shadow_cannot_issue_publication_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    registry = orchestrator.registry
    original_execute_isolated = registry._execute_isolated

    def self_removing_forged_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        monkeypatch.delattr(registry, "_execute_isolated")
        result = original_execute_isolated(*args, **kwargs)
        return result.model_copy(
            update={
                "output": {
                    **result.output,
                    "count": int(result.output["count"]) + 1,
                }
            }
        )

    monkeypatch.setattr(registry, "_execute_isolated", self_removing_forged_execute)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="self-removing forged registry result",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-self-removing-registry-dispatch",
    )

    assert "_execute_isolated" not in vars(registry)
    assert report.run_status == "failed_with_limitations"
    assert "product persistence refused: tool result lacks independent replay authority" in (
        report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_shadowed_runtime_guard_cannot_authorize_self_removing_registry_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path))
    registry = orchestrator.registry
    original_execute_isolated = registry._execute_isolated

    def self_removing_forged_execute(*args, **kwargs):  # type: ignore[no-untyped-def]
        monkeypatch.delattr(registry, "_execute_isolated")
        result = original_execute_isolated(*args, **kwargs)
        return result.model_copy(
            update={
                "output": {
                    **result.output,
                    "count": int(result.output["count"]) + 1,
                }
            }
        )

    monkeypatch.setattr(registry, "_execute_isolated", self_removing_forged_execute)
    monkeypatch.setattr(
        orchestrator,
        "_phase_tool_publication_runtime_is_exact",
        lambda: True,
    )
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="shadowed publication guard and self-removing registry dispatch",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={"tool_inputs": {"combos": {"hand_class": "AA"}}},
        ),
        run_id="run-product-shadowed-publication-guard",
    )

    assert "_phase_tool_publication_runtime_is_exact" in vars(orchestrator)
    assert "_execute_isolated" not in vars(registry)
    assert report.run_status == "failed_with_limitations"
    assert "product persistence refused: tool result lacks independent replay authority" in (
        report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_custom_tool_limits_round_trip_and_mismatched_reader_fails_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    range_text = ",".join(
        [
            "AA",
            "KK",
            "QQ",
            "JJ",
            "TT",
            "99",
            "88",
            "77",
            "AKs",
            "AQs",
            "AJs",
            "ATs",
            "KQs",
            "KJs",
            "QJs",
        ]
    )
    writer_policy = BudgetPolicyV2(max_tool_output_bytes=5_000)
    writer = Orchestrator(config, budget_policy=writer_policy)
    report = writer.run(
        CaseInput(
            kind="calculation",
            raw_text="custom tool output limit",
            analysis_scope="retrospective",
            requested_tools=["combos"],
            metadata={
                "tool_inputs": {
                    "combos": {
                        "range": range_text,
                        "dead_cards": [],
                    }
                }
            },
        ),
        run_id="run-product-custom-tool-limits",
    )
    result = next(item for item in report.tool_results if item.tool_name == "combos")

    assert report.run_status == "completed"
    assert 3_000 < canonical_json_utf8_size(result.output) <= 5_000
    assert writer._phase_tool_publication_runtime_is_exact()
    fresh_registry = writer._fresh_tool_verification_registry()
    assert (
        fresh_registry.max_payload_bytes,
        fresh_registry.max_output_bytes,
        fresh_registry.max_duration_seconds,
    ) == (
        writer_policy.max_tool_input_bytes,
        writer_policy.max_tool_output_bytes,
        min(30.0, writer_policy.max_runtime_seconds),
    )
    assert Orchestrator(config, budget_policy=writer_policy).load_report(report.run_id) == report

    writer.registry.max_output_bytes += 1
    assert not writer._phase_tool_publication_runtime_is_exact()

    mismatched_reader = Orchestrator(
        config,
        budget_policy=BudgetPolicyV2(max_tool_output_bytes=3_000),
    )
    with pytest.raises(ProductRunError) as caught:
        mismatched_reader.load_report(report.run_id)
    assert caught.value.failure.code in {
        ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED,
        ProductRunFailureCode.RUN_CORRUPT,
    }
