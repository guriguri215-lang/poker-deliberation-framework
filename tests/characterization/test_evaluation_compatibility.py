from __future__ import annotations

from pathlib import Path

from poker_deliberation.evaluation.models import (
    EVALUATION_CANONICALIZATION,
    EvaluationResultV1,
)
from poker_deliberation.evaluation.runner import run_evaluation
from poker_deliberation.runtime_conformance.models import CONFORMANCE_CANONICALIZATION
from poker_deliberation.schemas import FinalReport
from poker_deliberation.storage.terminal_models import VerifiedRunReadV2

ROOT = Path(__file__).resolve().parents[2]
SUITE = "evals/suites/p3_017a_v1.json"


def test_existing_product_artifact_models_remain_additively_unchanged() -> None:
    assert tuple(FinalReport.model_fields) == (
        "run_id",
        "run_status",
        "conclusion",
        "reconstructed_input",
        "data_quality",
        "claim_assessments",
        "analysis_sections",
        "agent_execution_records",
        "security_events",
        "tool_results",
        "alternatives",
        "sensitivity",
        "disputes",
        "evidence",
        "reproduction_steps",
        "approvals",
        "confidence",
        "limitations",
        "generated_at",
    )
    assert tuple(VerifiedRunReadV2.model_fields) == (
        "schema_version",
        "read_status",
        "run_id",
        "revision",
        "transaction_id",
        "current_pointer_sha256",
        "manifest_sha256",
        "inventory_sha256",
        "completion_marker_sha256",
        "resume_eligible",
        "budget_settlement_verified",
        "lifecycle_verified",
        "reachable_revisions",
        "pointer",
        "manifest",
        "completion_marker",
        "payloads",
    )
    assert not issubclass(EvaluationResultV1, FinalReport)
    assert not issubclass(EvaluationResultV1, VerifiedRunReadV2)


def test_evaluation_uses_a_dedicated_canonical_family_and_no_product_run_storage(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    del monkeypatch
    result = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id="a" * 40,
        source_tree_id="b" * 40,
    )

    assert result.canonicalization == EVALUATION_CANONICALIZATION
    assert EVALUATION_CANONICALIZATION != CONFORMANCE_CANONICALIZATION
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "revision-runs").exists()
