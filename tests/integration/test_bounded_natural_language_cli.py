from __future__ import annotations

from pathlib import Path

from poker_deliberation.bounded_natural_language_models import (
    BoundedIntakePreparationResultV1,
)
from poker_deliberation.cli import main
from poker_deliberation.storage.revision_canonical import parse_canonical_model
from tests.bounded_natural_language_support import SOURCE_BYTES


def test_three_stage_bounded_cli_requires_all_confirmation_hashes(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source = tmp_path / "source.txt"
    preparation_path = tmp_path / "preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    source.write_bytes(SOURCE_BYTES)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", "runtime/legacy")
    monkeypatch.setenv("POKER_DELIBERATION_REVISION_RUNS_DIR", "runtime/product")
    monkeypatch.setenv(
        "POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR",
        "runtime/budget",
    )

    assert (
        main(
            [
                "prepare-bounded-review-intake",
                "--source",
                str(source),
                "--output",
                str(preparation_path),
                "--intake-id",
                "intake-bounded-cli-1",
                "--source-id",
                "fixture-bounded-cli-1",
                "--source-kind",
                "repository_fixture",
                "--license-classification",
                "repository_owned_mit",
                "--usage-classification",
                "redistribution_allowed",
                "--classification",
                "public",
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    prepared = parse_canonical_model(
        preparation_path.read_bytes(), BoundedIntakePreparationResultV1
    )
    assert prepared.source is not None and prepared.candidate is not None
    projection = prepared.candidate.projection

    assert (
        main(
            [
                "confirm-bounded-review-intake",
                "--preparation",
                str(preparation_path),
                "--output",
                str(confirmation_path),
                "--run-id",
                "run-bounded-cli-1",
                "--authority-id",
                "local-cli-user",
                "--confirmation-id",
                "confirmation-bounded-cli-1",
                "--idempotency-key",
                "idempotency-bounded-cli-1",
                "--expected-source-sha256",
                prepared.source.content_sha256,
                "--expected-candidate-sha256",
                prepared.candidate.candidate_sha256,
                "--expected-source-bindings-sha256",
                projection.source_bindings_sha256,
                "--expected-focal-sha256",
                projection.focal_decision.focal_sha256,
                "--expected-tool-plan-sha256",
                projection.tool_plan.tool_plan_sha256,
                "--expected-extractor-sha256",
                projection.extractor_sha256,
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "review-bounded-confirmed-intake",
                "--source",
                str(source),
                "--preparation",
                str(preparation_path),
                "--confirmation",
                str(confirmation_path),
                "--format",
                "summary",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "run-bounded-cli-1" in output
    assert "completed" in output


def test_bounded_cli_refuses_one_wrong_confirmation_hash(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "source.txt"
    preparation_path = tmp_path / "preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    source.write_bytes(SOURCE_BYTES)
    assert (
        main(
            [
                "prepare-bounded-review-intake",
                "--source",
                str(source),
                "--output",
                str(preparation_path),
                "--intake-id",
                "intake-bounded-cli-tamper",
                "--source-id",
                "fixture-bounded-cli-tamper",
            ]
        )
        == 0
    )
    capsys.readouterr()
    prepared = parse_canonical_model(
        preparation_path.read_bytes(), BoundedIntakePreparationResultV1
    )
    assert prepared.source is not None and prepared.candidate is not None
    projection = prepared.candidate.projection
    result = main(
        [
            "confirm-bounded-review-intake",
            "--preparation",
            str(preparation_path),
            "--output",
            str(confirmation_path),
            "--run-id",
            "run-bounded-cli-tamper",
            "--authority-id",
            "local-cli-user",
            "--confirmation-id",
            "confirmation-bounded-cli-tamper",
            "--idempotency-key",
            "idempotency-bounded-cli-tamper",
            "--expected-source-sha256",
            prepared.source.content_sha256,
            "--expected-candidate-sha256",
            prepared.candidate.candidate_sha256,
            "--expected-source-bindings-sha256",
            projection.source_bindings_sha256,
            "--expected-focal-sha256",
            projection.focal_decision.focal_sha256,
            "--expected-tool-plan-sha256",
            "0" * 64,
            "--expected-extractor-sha256",
            projection.extractor_sha256,
        ]
    )
    assert result == 2
    assert "BNL_E_CONFIRMATION_BINDING" in capsys.readouterr().err
    assert not confirmation_path.exists()


def test_bounded_cli_rejects_invalid_secret_authority_without_echo(
    tmp_path: Path,
    capsys,
) -> None:
    source = tmp_path / "source.txt"
    preparation_path = tmp_path / "preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    source.write_bytes(SOURCE_BYTES)
    assert (
        main(
            [
                "prepare-bounded-review-intake",
                "--source",
                str(source),
                "--output",
                str(preparation_path),
                "--intake-id",
                "intake-bounded-cli-authority",
                "--source-id",
                "fixture-bounded-cli-authority",
            ]
        )
        == 0
    )
    capsys.readouterr()
    prepared = parse_canonical_model(
        preparation_path.read_bytes(), BoundedIntakePreparationResultV1
    )
    assert prepared.source is not None and prepared.candidate is not None
    projection = prepared.candidate.projection
    secret = "sk-abcdefghijklmnopqrstuvwxyz?"

    result = main(
        [
            "confirm-bounded-review-intake",
            "--preparation",
            str(preparation_path),
            "--output",
            str(confirmation_path),
            "--run-id",
            "run-bounded-cli-authority",
            "--authority-id",
            secret,
            "--confirmation-id",
            "confirmation-bounded-cli-authority",
            "--idempotency-key",
            "idempotency-bounded-cli-authority",
            "--expected-source-sha256",
            prepared.source.content_sha256,
            "--expected-candidate-sha256",
            prepared.candidate.candidate_sha256,
            "--expected-source-bindings-sha256",
            projection.source_bindings_sha256,
            "--expected-focal-sha256",
            projection.focal_decision.focal_sha256,
            "--expected-tool-plan-sha256",
            projection.tool_plan.tool_plan_sha256,
            "--expected-extractor-sha256",
            projection.extractor_sha256,
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "BNL_E_CONTROL" in captured.err
    assert secret not in captured.err
    assert not confirmation_path.exists()
