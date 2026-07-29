from __future__ import annotations

import json
from pathlib import Path

from poker_deliberation.cli import main
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    ReviewIntakePreparationResultV1,
)
from poker_deliberation.storage.revision_canonical import parse_canonical_model
from tests.confirmed_review_support import SOURCE_BYTES, candidate_payload


def test_three_stage_confirmed_review_cli_requires_explicit_hash_confirmation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source_path = tmp_path / "source.txt"
    candidate_path = tmp_path / "candidate.json"
    preparation_path = tmp_path / "preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    source_path.write_bytes(SOURCE_BYTES)
    candidate_path.write_text(
        json.dumps(candidate_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
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
                "prepare-review-intake",
                "--source",
                str(source_path),
                "--candidate",
                str(candidate_path),
                "--output",
                str(preparation_path),
                "--source-id",
                "source-cli-test-1",
                "--format",
                "json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    preparation = parse_canonical_model(
        preparation_path.read_bytes(),
        ReviewIntakePreparationResultV1,
    )
    assert preparation.source is not None
    assert preparation.candidate is not None

    assert (
        main(
            [
                "confirm-review-intake",
                "--preparation",
                str(preparation_path),
                "--output",
                str(confirmation_path),
                "--run-id",
                "run-confirmed-cli-test-1",
                "--authority-id",
                "local-cli-test-user",
                "--confirmation-id",
                "confirmation-cli-test-1",
                "--idempotency-key",
                "idempotency-cli-test-1",
                "--expected-source-sha256",
                preparation.source.content_sha256,
                "--expected-candidate-sha256",
                preparation.candidate.candidate_sha256,
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
                "review-confirmed-intake",
                "--source",
                str(source_path),
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
    assert "run-confirmed-cli-test-1" in output
    assert "completed" in output


def test_confirmation_cli_refuses_hash_mismatch_without_output(
    tmp_path: Path,
    capsys,
) -> None:
    source_path = tmp_path / "source.txt"
    candidate_path = tmp_path / "candidate.json"
    preparation_path = tmp_path / "preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    source_path.write_bytes(SOURCE_BYTES)
    candidate_path.write_text(json.dumps(candidate_payload()), encoding="utf-8")
    assert (
        main(
            [
                "prepare-review-intake",
                "--source",
                str(source_path),
                "--candidate",
                str(candidate_path),
                "--output",
                str(preparation_path),
                "--source-id",
                "source-cli-test-2",
            ]
        )
        == 0
    )
    capsys.readouterr()
    preparation = parse_canonical_model(
        preparation_path.read_bytes(),
        ReviewIntakePreparationResultV1,
    )
    assert preparation.candidate is not None
    assert (
        main(
            [
                "confirm-review-intake",
                "--preparation",
                str(preparation_path),
                "--output",
                str(confirmation_path),
                "--run-id",
                "run-confirmed-cli-test-2",
                "--authority-id",
                "local-cli-test-user",
                "--confirmation-id",
                "confirmation-cli-test-2",
                "--idempotency-key",
                "idempotency-cli-test-2",
                "--expected-source-sha256",
                "0" * 64,
                "--expected-candidate-sha256",
                preparation.candidate.candidate_sha256,
            ]
        )
        == 2
    )
    assert not confirmation_path.exists()
    assert "CRI_E_CONFIRMATION_BINDING" in capsys.readouterr().err


def test_confirmation_cli_rejects_oversized_preparation_before_parsing(
    tmp_path: Path,
    capsys,
) -> None:
    preparation_path = tmp_path / "oversized-preparation.json"
    confirmation_path = tmp_path / "confirmation.json"
    preparation_path.write_bytes(b"x" * (MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES + 1))

    assert (
        main(
            [
                "confirm-review-intake",
                "--preparation",
                str(preparation_path),
                "--output",
                str(confirmation_path),
                "--run-id",
                "run-confirmed-cli-size-1",
                "--authority-id",
                "local-cli-test-user",
                "--confirmation-id",
                "confirmation-cli-size-1",
                "--idempotency-key",
                "idempotency-cli-size-1",
                "--expected-source-sha256",
                "0" * 64,
                "--expected-candidate-sha256",
                "0" * 64,
            ]
        )
        == 2
    )
    assert not confirmation_path.exists()
    assert "CRI_E_SOURCE_SIZE" in capsys.readouterr().err
