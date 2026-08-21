from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from poker_deliberation import bounded_river_review_workflow_evaluation as evaluation
from poker_deliberation import bounded_river_review_workflow_qualification as qualification
from poker_deliberation.bounded_river_review_workflow_evaluation import (
    load_bounded_river_review_workflow_evaluation_result_v2,
    load_bounded_river_review_workflow_fixture,
    load_bounded_river_review_workflow_fixture_v2,
    run_bounded_river_review_workflow_evaluation,
    run_bounded_river_review_workflow_evaluation_v2,
    verify_bounded_river_review_workflow_evaluation_result_v2,
)
from poker_deliberation.bounded_river_review_workflow_qualification import (
    build_sanitized_bounded_river_review_workflow_qualification_manifest,
    load_sanitized_bounded_river_review_workflow_qualification_manifest,
    write_sanitized_bounded_river_review_workflow_qualification_manifest,
)
from poker_deliberation.codex_bridge import product as bridge_product
from scripts import run_bounded_river_review_workflow_evaluation as evaluation_runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v1"
FIXTURE_V2_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v2"


def test_v2_runner_rejects_overlap_and_unignored_targets_before_write() -> None:
    ignored_root = REPOSITORY_ROOT / "tmp" / "codex-goals" / "p3-030g"
    token = uuid4().hex[:8]
    work_root = ignored_root / f"w-{token[:4]}"
    output = ignored_root / f"path-check-result-{token}.json"
    manifest = ignored_root / f"path-check-manifest-{token}.json"
    unignored = REPOSITORY_ROOT / f"p3-030g-unignored-{token}"
    common = {
        "fixture": FIXTURE_V2_ROOT / "scenarios.json",
        "source": FIXTURE_ROOT / "source-ja.txt",
        "range_path": FIXTURE_ROOT / "range.json",
    }

    assert evaluation_runner._validate_runner_paths(
        **common,
        work_root=work_root,
        output=output,
        manifest_output=manifest,
    ) == (work_root.resolve(), output.resolve(), manifest.resolve())
    for unsafe_output, unsafe_manifest in (
        (output, output),
        (FIXTURE_ROOT / "source-ja.txt", manifest),
        (work_root / "result.json", manifest),
        (output, work_root / "manifest.json"),
        (REPOSITORY_ROOT / "user_materials" / f"result-{token}.json", manifest),
    ):
        with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
            evaluation_runner._validate_runner_paths(
                **common,
                work_root=work_root,
                output=unsafe_output,
                manifest_output=unsafe_manifest,
            )
    with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
        evaluation_runner._validate_runner_paths(
            **common,
            work_root=work_root,
            output=work_root / "result.json",
            manifest_output=manifest,
        )
    existing = ignored_root / f"path-check-existing-{token}"
    existing.mkdir(parents=True)
    try:
        with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
            evaluation_runner._validate_runner_paths(
                **common,
                work_root=existing,
                output=output,
                manifest_output=manifest,
            )
    finally:
        existing.rmdir()
    outside_work = REPOSITORY_ROOT.parent / f"p3-030g-outside-{token}"
    with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
        evaluation_runner._validate_runner_paths(
            **common,
            work_root=outside_work,
            output=output,
            manifest_output=manifest,
        )
    parent_file = ignored_root / f"path-check-parent-{token}"
    parent_file.parent.mkdir(parents=True, exist_ok=True)
    parent_file.write_bytes(b"fixture")
    try:
        with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
            evaluation_runner._validate_runner_paths(
                **common,
                work_root=parent_file / "work",
                output=output,
                manifest_output=manifest,
            )
    finally:
        parent_file.unlink()
    with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
        evaluation_runner._validate_runner_paths(
            **common,
            work_root=unignored,
            output=output,
            manifest_output=manifest,
        )
    assert not work_root.exists()
    assert not output.exists()
    assert not manifest.exists()
    assert not unignored.exists()
    assert not outside_work.exists()


def test_v2_runner_uses_production_runtime_confinement_before_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex[:8]
    work = REPOSITORY_ROOT / "tmp" / "codex-goals" / "p3-030g" / f"c-{token[:4]}"
    output = work.parent / f"confine-result-{token}.json"
    called: list[Path] = []

    def refuse(path: Path, repository_root: Path) -> Path:
        called.append(path)
        raise evaluation_runner.BridgeProductError("refused")

    monkeypatch.setattr(evaluation_runner, "confined_runtime_scratch_path", refuse)
    with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
        evaluation_runner._validate_runner_paths(
            fixture=FIXTURE_V2_ROOT / "scenarios.json",
            source=FIXTURE_ROOT / "source-ja.txt",
            range_path=FIXTURE_ROOT / "range.json",
            work_root=work,
            output=output,
            manifest_output=None,
        )
    assert called == [work.resolve()]
    assert not work.exists()
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
def test_v2_runner_rejects_terminal_store_path_overflow_before_write() -> None:
    token = uuid4().hex[:8]
    ignored_root = REPOSITORY_ROOT / "tmp" / "codex-goals" / "p3-030g"
    work = ignored_root / f"terminal-store-path-overflow-{token}"
    output = ignored_root / f"path-overflow-result-{token}.json"

    with pytest.raises(ValueError, match=r"^BRWE_E_PATH$"):
        evaluation_runner._validate_runner_paths(
            fixture=FIXTURE_V2_ROOT / "scenarios.json",
            source=FIXTURE_ROOT / "source-ja.txt",
            range_path=FIXTURE_ROOT / "range.json",
            work_root=work,
            output=output,
            manifest_output=None,
        )
    assert not work.exists()
    assert not output.exists()


def test_v2_runner_allows_outputs_outside_repository(tmp_path: Path) -> None:
    ignored_work = REPOSITORY_ROOT / "tmp" / "codex-goals" / "p3-030g" / f"o-{uuid4().hex[:4]}"
    output = tmp_path / "result.json"
    manifest = tmp_path / "manifest.json"

    assert evaluation_runner._validate_runner_paths(
        fixture=FIXTURE_V2_ROOT / "scenarios.json",
        source=FIXTURE_ROOT / "source-ja.txt",
        range_path=FIXTURE_ROOT / "range.json",
        work_root=ignored_work,
        output=output,
        manifest_output=manifest,
    ) == (ignored_work.resolve(), output.resolve(), manifest.resolve())
    assert not output.exists()
    assert not manifest.exists()


def test_v2_result_unknown_is_not_pass_and_retains_expected_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case_evidence = (
        ("a" * 64, "b" * 64, "c" * 64),
        ("d" * 64, "e" * 64),
        ("f" * 64, *(f"{index:064x}" for index in range(1, 7))),
        ("1" * 64,),
        ("2" * 64, "3" * 64),
        ("4" * 64, "4" * 64),
    )
    cases = tuple(
        evaluation.BoundedRiverReviewWorkflowCaseV2(
            case_id=case_id,
            status="UNKNOWN" if index == 0 else "pass",
            passed=index != 0,
            failure_code="BRWE_E_EVIDENCE_UNKNOWN" if index == 0 else None,
            expected_evidence_sha256=evidence_values,
            observed_evidence_sha256=() if index == 0 else evidence_values,
        )
        for index, (case_id, evidence_values) in enumerate(
            zip(evaluation._CASE_ORDER_V2, case_evidence, strict=True)
        )
    )
    metrics = tuple(
        evaluation.BoundedRiverReviewWorkflowMetricV2(
            metric=metric,
            status=case.status,
            passed=case.passed,
            failure_code=case.failure_code,
            expected_evidence_sha256=case.expected_evidence_sha256,
            observed_evidence_sha256=case.observed_evidence_sha256,
        )
        for metric, case in zip(evaluation._METRIC_ORDER_V2, cases, strict=True)
    )
    payload: dict[str, object] = {
        "schema_version": "2.0.0",
        "evaluation_id": "p3-030g-bounded-river-review-workflow-evaluation-v2",
        "fixture_id": "p3-030g-bounded-river-review-workflow-v2",
        "fixture_sha256": "a" * 64,
        "source_fixture_sha256": "5" * 64,
        "range_fixture_sha256": "6" * 64,
        "range_definition_sha256": "7" * 64,
        "source_commit_id": "1" * 40,
        "source_tree_id": "2" * 40,
        "workflow_id": "unknown-evaluation",
        "plan_sha256": "b" * 64,
        "workflow_confirmation_sha256": "e" * 64,
        "linkage_sha256": "8" * 64,
        "source_terminal_manifest_sha256": "c" * 64,
        "source_terminal_inventory_sha256": "9" * 64,
        "bridge_terminal_manifest_sha256": "a" * 64,
        "bridge_terminal_inventory_sha256": "b" * 64,
        "final_report_artifact_sha256": "3" * 64,
        "confirmation_hashes_sha256": "d" * 64,
        "role_confirmation_receipts_sha256": "f" * 64,
        "role_confirmation_fields_sha256": tuple(f"{index:064x}" for index in range(1, 6)),
        "all_confirmation_field_mutations_sha256": f"{6:064x}",
        "p2_artifact_lineage_sha256": "1" * 64,
        "terminal_replay_report_sha256": "2" * 64,
        "runtime_source_inventory_sha256": "4" * 64,
        "cases": cases,
        "metrics": metrics,
        "score_milli": 833,
        "status": "UNKNOWN",
        "passed": False,
        "transport_qualification": "deterministic_fixture",
        "live_qualification_status": "UNKNOWN",
        "actual_backend_model_input": "UNKNOWN",
        "api_live_executed": False,
    }
    result = evaluation.BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
        {
            **payload,
            "result_sha256": evaluation.canonical_domain_sha256(
                evaluation._RESULT_V2_HASH_DOMAIN,
                payload,
            ),
        },
        strict=True,
    )

    assert result.status == "UNKNOWN"
    assert result.passed is False
    assert result.cases[0].expected_evidence_sha256
    assert not result.cases[0].observed_evidence_sha256

    fixture_path = FIXTURE_V2_ROOT / "scenarios.json"
    source_path = FIXTURE_ROOT / "source-ja.txt"
    range_path = FIXTURE_ROOT / "range.json"
    fixture_sha256 = evaluation.sha256_bytes(fixture_path.read_bytes())
    source_sha256 = evaluation.sha256_bytes(source_path.read_bytes())
    range_file_sha256 = evaluation.sha256_bytes(range_path.read_bytes())
    range_definition_sha256 = evaluation.sha256_bytes(range_path.read_bytes().removesuffix(b"\n"))
    runtime_sha256 = evaluation.bridge_runtime_source_inventory_sha256(REPOSITORY_ROOT)
    rebound_payload = result.model_dump(mode="python")
    rebound_cases = list(result.cases)
    rebound_metrics = list(result.metrics)
    rebound_cases[0] = rebound_cases[0].model_copy(
        update={
            "expected_evidence_sha256": (
                fixture_sha256,
                result.plan_sha256,
                result.source_terminal_manifest_sha256,
            )
        }
    )
    rebound_metrics[0] = rebound_metrics[0].model_copy(
        update={"expected_evidence_sha256": rebound_cases[0].expected_evidence_sha256}
    )
    for items in (rebound_cases, rebound_metrics):
        items[5] = items[5].model_copy(
            update={
                "expected_evidence_sha256": (runtime_sha256, runtime_sha256),
                "observed_evidence_sha256": (runtime_sha256, runtime_sha256),
            }
        )
    rebound_payload.update(
        {
            "fixture_sha256": fixture_sha256,
            "source_fixture_sha256": source_sha256,
            "range_fixture_sha256": range_file_sha256,
            "range_definition_sha256": range_definition_sha256,
            "runtime_source_inventory_sha256": runtime_sha256,
            "cases": tuple(rebound_cases),
            "metrics": tuple(rebound_metrics),
        }
    )
    rebound_payload["result_sha256"] = evaluation.canonical_domain_sha256(
        evaluation._RESULT_V2_HASH_DOMAIN,
        {key: value for key, value in rebound_payload.items() if key != "result_sha256"},
    )
    rebound = evaluation.BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
        rebound_payload,
        strict=True,
    )
    monkeypatch.setattr(evaluation, "_verify_v2_repository_identity", lambda *args, **kwargs: None)
    trusted_context = {
        "repository_root": REPOSITORY_ROOT,
        "fixture_path": fixture_path,
        "source_path": source_path,
        "range_path": range_path,
        "source_commit_id": "1" * 40,
        "source_tree_id": "2" * 40,
    }
    assert evaluation.verify_bounded_river_review_workflow_evaluation_result_v2(
        rebound,
        **trusted_context,
    )
    forged_payload = rebound.model_dump(mode="python")
    forged_payload["source_fixture_sha256"] = "0" * 64
    forged_payload["result_sha256"] = evaluation.canonical_domain_sha256(
        evaluation._RESULT_V2_HASH_DOMAIN,
        {key: value for key, value in forged_payload.items() if key != "result_sha256"},
    )
    forged = evaluation.BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
        forged_payload,
        strict=True,
    )
    assert not evaluation.verify_bounded_river_review_workflow_evaluation_result_v2(
        forged,
        **trusted_context,
    )


def test_v2_deterministic_executor_rejects_wrong_or_extra_kwargs(tmp_path: Path) -> None:
    config = evaluation.bounded_river_review_workflow_evaluation_config(tmp_path)
    repository = REPOSITORY_ROOT
    bridge_root = tmp_path / "bridge"
    runtime_root = tmp_path / "runtime"
    kwargs: dict[str, object] = {
        "config": config,
        "repository_root": repository,
        "bridge_root": bridge_root,
        "runtime_root": runtime_root,
        "bridge_run_id": "bridge-v2",
        "role": evaluation.BRIDGE_ROLE_ORDER[0],
        "auth_mode": evaluation.RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        "codex_binary": None,
    }
    expected = {
        "config": config,
        "repository_root": repository,
        "bridge_root": bridge_root,
        "runtime_root": runtime_root,
        "bridge_run_id": "bridge-v2",
        "role": evaluation.BRIDGE_ROLE_ORDER[0],
    }

    assert evaluation._deterministic_role_executor_kwargs_match(kwargs, **expected)
    for key, wrong in (
        ("bridge_run_id", "wrong-run"),
        ("role", evaluation.BRIDGE_ROLE_ORDER[1]),
        ("auth_mode", evaluation.RuntimeAuthModeV1.OPENAI_API),
        ("codex_binary", tmp_path / "codex.exe"),
        ("repository_root", str(repository)),
        ("bridge_root", str(bridge_root)),
        ("runtime_root", str(runtime_root)),
        ("runtime_root", runtime_root / ".." / "runtime"),
    ):
        mutated = dict(kwargs)
        mutated[key] = wrong
        assert not evaluation._deterministic_role_executor_kwargs_match(mutated, **expected)
    mutated = {**kwargs, "unexpected": "value"}
    assert not evaluation._deterministic_role_executor_kwargs_match(mutated, **expected)


def test_deterministic_workflow_evaluation_scores_all_independent_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = REPOSITORY_ROOT / "tmp" / f"we-{uuid4().hex[:8]}"
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    try:
        fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture(
            FIXTURE_ROOT / "scenarios.json"
        )
        result = run_bounded_river_review_workflow_evaluation(
            fixture,
            fixture_sha256=fixture_sha256,
            source_path=FIXTURE_ROOT / "source-ja.txt",
            range_path=FIXTURE_ROOT / "range.json",
            repository_root=REPOSITORY_ROOT,
            work_root=work_root,
            source_commit_id="1" * 40,
            source_tree_id="2" * 40,
        )

        assert result.passed is True
        assert result.score_milli == 1000
        assert len(result.metrics) == 5
        assert all(metric.passed and metric.evidence for metric in result.metrics)
        assert result.result_sha256 != "0" * 64
    finally:
        if work_root.exists():
            shutil.rmtree(work_root)


def test_v2_evaluator_runs_production_supervised_lifecycle_and_self_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = REPOSITORY_ROOT / "tmp" / f"we-v2-{uuid4().hex[:8]}"
    result_path = work_root.parent / f"we-v2-result-{uuid4().hex[:8]}.json"
    manifest_path = work_root.parent / f"we-v2-manifest-{uuid4().hex[:8]}.json"
    monkeypatch.setattr(evaluation, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(evaluation, "verify_bridge_module_origins", lambda *args, **kwargs: None)
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(qualification, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        qualification,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    try:
        fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture_v2(
            FIXTURE_V2_ROOT / "scenarios.json"
        )
        result = run_bounded_river_review_workflow_evaluation_v2(
            fixture,
            fixture_sha256=fixture_sha256,
            source_path=FIXTURE_ROOT / "source-ja.txt",
            range_path=FIXTURE_ROOT / "range.json",
            repository_root=REPOSITORY_ROOT,
            work_root=work_root,
            source_commit_id="1" * 40,
            source_tree_id="2" * 40,
        )

        assert result.passed is True
        assert result.status == "pass"
        assert result.score_milli == 1000
        assert result.transport_qualification == "deterministic_fixture"
        assert result.live_qualification_status == "UNKNOWN"
        assert result.actual_backend_model_input == "UNKNOWN"
        assert result.api_live_executed is False
        assert all(
            item.passed
            and item.expected_evidence_sha256
            and item.expected_evidence_sha256 == item.observed_evidence_sha256
            for item in result.cases
        )
        assert all(
            item.passed
            and item.expected_evidence_sha256
            and item.expected_evidence_sha256 == item.observed_evidence_sha256
            for item in result.metrics
        )
        trusted_context = {
            "repository_root": REPOSITORY_ROOT,
            "fixture_path": FIXTURE_V2_ROOT / "scenarios.json",
            "source_path": FIXTURE_ROOT / "source-ja.txt",
            "range_path": FIXTURE_ROOT / "range.json",
            "source_commit_id": "1" * 40,
            "source_tree_id": "2" * 40,
        }
        assert verify_bounded_river_review_workflow_evaluation_result_v2(
            result,
            **trusted_context,
        )

        forged_payload = result.model_dump(mode="python")
        forged_payload["source_fixture_sha256"] = "0" * 64
        forged_payload["result_sha256"] = evaluation.canonical_domain_sha256(
            evaluation._RESULT_V2_HASH_DOMAIN,
            {key: value for key, value in forged_payload.items() if key != "result_sha256"},
        )
        forged = evaluation.BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
            forged_payload,
            strict=True,
        )
        assert not verify_bounded_river_review_workflow_evaluation_result_v2(
            forged,
            **trusted_context,
        )

        manifest = build_sanitized_bounded_river_review_workflow_qualification_manifest(
            config=evaluation.bounded_river_review_workflow_evaluation_config(work_root),
            repository_root=REPOSITORY_ROOT,
            workflow_root=work_root / "w",
            workflow_id=result.workflow_id,
            qualification_id="p3-030g-integration-qualification",
            deterministic_evaluation=result,
        )
        write_sanitized_bounded_river_review_workflow_qualification_manifest(
            manifest_path,
            manifest,
        )
        loaded_manifest = load_sanitized_bounded_river_review_workflow_qualification_manifest(
            manifest_path
        )
        assert loaded_manifest == manifest
        assert manifest.deterministic_evaluation.result_sha256 == result.result_sha256
        assert manifest.lineage.plan_sha256 == result.plan_sha256
        assert (
            manifest.lineage.current_bridge_manifest_sha256
            == result.bridge_terminal_manifest_sha256
        )
        assert manifest.terminal.final_report_artifact_sha256 == result.final_report_artifact_sha256
        manifest_text = manifest_path.read_text(encoding="utf-8")
        assert all(
            excluded not in manifest_text
            for excluded in (
                "outbound_canonical_utf8",
                "credential_value",
                "system_prompt",
                "model_trace",
                "user_materials",
            )
        )

        anchor_tamper = result.model_dump(mode="python")
        anchor_tamper["fixture_sha256"] = "0" * 64
        anchor_tamper["result_sha256"] = evaluation.canonical_domain_sha256(
            evaluation._RESULT_V2_HASH_DOMAIN,
            {key: value for key, value in anchor_tamper.items() if key != "result_sha256"},
        )
        with pytest.raises(ValueError, match="result mismatch"):
            evaluation.BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
                anchor_tamper,
                strict=True,
            )

        result_path.write_bytes(evaluation.canonical_json_bytes(result))
        assert load_bounded_river_review_workflow_evaluation_result_v2(result_path) == result
        tampered = result.model_dump(mode="json")
        tampered["score_milli"] = 999
        result_path.write_bytes(evaluation.canonical_json_bytes(tampered))
        with pytest.raises(evaluation.BoundedRiverReviewWorkflowEvaluationError):
            load_bounded_river_review_workflow_evaluation_result_v2(result_path)
    finally:
        if work_root.exists():
            shutil.rmtree(work_root)
        result_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
