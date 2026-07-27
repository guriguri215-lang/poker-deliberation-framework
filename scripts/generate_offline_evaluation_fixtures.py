"""Generate or verify the canonical P3-017A repository-owned fixture."""

# ruff: noqa: E402 -- insert the repository src path before importing package code.

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from poker_deliberation.evaluation.canonical import (
    CASE_INPUT_DOMAIN,
    DATASET_CONTENT_DOMAIN,
    canonical_domain_sha256,
    canonical_json_bytes,
    sha256_bytes,
)
from poker_deliberation.evaluation.models import (
    DatasetManifestV1,
    EvaluationCaseInputV1,
    EvaluationCaseV1,
    EvaluationDatasetV1,
    EvaluationSuiteV1,
    ExpectedEvidenceV1,
    ScorerConfigV1,
)

DATASET_RELATIVE = Path("evals/datasets/p3_017a/v1/cases.json")
MANIFEST_RELATIVE = Path("evals/datasets/p3_017a/v1/manifest.json")
SCORER_RELATIVE = Path("evals/scorers/exact_evidence_match_v1.json")
SUITE_RELATIVE = Path("evals/suites/p3_017a_v1.json")


def _case(
    case_id: str,
    case_kind: str,
    mutation: str,
    evidence: tuple[str, ...],
    **inputs: object,
) -> EvaluationCaseV1:
    case_input = EvaluationCaseInputV1(
        scenario=case_kind,  # type: ignore[arg-type]
        mutation=mutation,  # type: ignore[arg-type]
        **inputs,
    )
    return EvaluationCaseV1(
        case_id=case_id,
        case_kind=case_kind,  # type: ignore[arg-type]
        input=case_input,
        input_sha256=canonical_domain_sha256(CASE_INPUT_DOMAIN, case_input),
        expected_evidence=ExpectedEvidenceV1(tokens=evidence),
    )


def fixture_documents() -> tuple[
    EvaluationDatasetV1,
    DatasetManifestV1,
    ScorerConfigV1,
    EvaluationSuiteV1,
]:
    dataset = EvaluationDatasetV1(
        dataset_id="p3-017a-synthetic",
        dataset_version="1.0.0",
        cases=(
            _case(
                "case-01-normal",
                "normal",
                "none",
                (
                    "calculator:oracle-match",
                    "calculator:pot_odds:floating-verified",
                    "context:semantics-preserved",
                    "epistemic-label:calculated",
                    "external-effect:false",
                    "routing:python-orchestrator",
                    "runtime-bridge:false",
                ),
                tool_name="pot_odds",
                pot_before_bet=100,
                opponent_bet=50,
                call_cost=50,
                expected_rake=0,
                oracle_numerator=1,
                oracle_denominator=4,
            ),
            _case(
                "case-02-context-provenance",
                "context-provenance-mismatch",
                "change-context-source",
                ("runtime-conformance:context-provenance-mismatch",),
            ),
            _case(
                "case-03-role-allowlist",
                "role-allowlist-mismatch",
                "expand-tool-allowlist",
                ("runtime-conformance:allowlist-semantic-mismatch",),
            ),
            _case(
                "case-04-calculator-oracle",
                "calculator-oracle-mismatch",
                "change-oracle",
                ("calculator:oracle-mismatch",),
                tool_name="pot_odds",
                pot_before_bet=100,
                opponent_bet=50,
                call_cost=50,
                expected_rake=0,
                oracle_numerator=1,
                oracle_denominator=5,
            ),
            _case(
                "case-05-missing-denominator",
                "missing-denominator",
                "remove-denominator-policy",
                ("contract-rejection:denominator_policy",),
            ),
            _case(
                "case-06-missing-scorer",
                "missing-scorer",
                "remove-scorer-path",
                ("contract-rejection:scorer_path",),
            ),
            _case(
                "case-07-missing-version",
                "missing-version",
                "remove-schema-version",
                ("contract-rejection:dataset_version",),
            ),
            _case(
                "case-08-unsupported-solver",
                "unsupported-solver-claim",
                "claim-equilibrium-without-evidence",
                (
                    "epistemic-label:unknown",
                    "solver-claim:rejected",
                    "solver-status:unavailable",
                ),
                tool_name="solver_status",
            ),
            _case(
                "case-09-secret-metadata",
                "synthetic-secret-metadata",
                "insert-synthetic-secret-shape",
                ("metadata-canary:rejected",),
            ),
            _case(
                "case-10-structured-timeout",
                "structured-timeout",
                "exceed-declared-timeout",
                ("external-effect:false", "timeout:structured"),
                timeout_ms=50,
                simulated_elapsed_ms=51,
            ),
        ),
    )
    license_bytes = (ROOT / "LICENSE").read_bytes()
    manifest = DatasetManifestV1(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        ownership="repository-owned",
        license_spdx="MIT",
        license_path="LICENSE",
        license_sha256=sha256_bytes(license_bytes),
        cases_path=DATASET_RELATIVE.as_posix(),
        case_count=len(dataset.cases),
        content_sha256=canonical_domain_sha256(DATASET_CONTENT_DOMAIN, dataset),
    )
    scorer = ScorerConfigV1(
        scorer_id="exact-evidence-match",
        scorer_version="1.0.0",
        metric_id="reproducibility",
        direction="higher-is-better",
        aggregation="micro-mean",
        denominator_policy="all-declared-cases",
        invalid_or_missing_count_policy="fail-closed",
        threshold="1.0",
        human_review_rubric=None,
    )
    suite = EvaluationSuiteV1(
        suite_id="p3-017a-offline-integrated",
        suite_version="1.0.0",
        dataset_manifest_path=MANIFEST_RELATIVE.as_posix(),
        dataset_manifest_sha256=sha256_bytes(canonical_json_bytes(manifest)),
        scorer_path=SCORER_RELATIVE.as_posix(),
        scorer_sha256=sha256_bytes(canonical_json_bytes(scorer)),
        evaluation_time_utc="2026-07-27T00:00:00Z",
    )
    return dataset, manifest, scorer, suite


def _outputs() -> dict[Path, bytes]:
    dataset, manifest, scorer, suite = fixture_documents()
    return {
        ROOT / DATASET_RELATIVE: canonical_json_bytes(dataset),
        ROOT / MANIFEST_RELATIVE: canonical_json_bytes(manifest),
        ROOT / SCORER_RELATIVE: canonical_json_bytes(scorer),
        ROOT / SUITE_RELATIVE: canonical_json_bytes(suite),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    outputs = _outputs()
    if args.check:
        stale = [
            path
            for path, data in outputs.items()
            if not path.is_file() or path.read_bytes() != data
        ]
        if stale:
            for path in stale:
                print(f"out of date: {path}")
            return 1
        for path in outputs:
            print(f"up to date: {path}")
        return 0
    for path, data in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
