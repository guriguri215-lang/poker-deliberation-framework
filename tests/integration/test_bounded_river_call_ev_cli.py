from __future__ import annotations

from pathlib import Path

from poker_deliberation.bounded_river_call_ev_models import (
    BoundedRiverCallEvPreparationResultV1,
)
from poker_deliberation.cli import main
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    parse_canonical_model,
)
from tests.bounded_river_call_ev_support import (
    candidate_hashes,
    range_definition,
    river_source,
)


def _prepare(
    tmp_path: Path,
    capsys,
) -> tuple[Path, Path, Path, BoundedRiverCallEvPreparationResultV1]:
    source = tmp_path / "river.txt"
    range_path = tmp_path / "range.json"
    preparation = tmp_path / "preparation.json"
    source.write_bytes(river_source())
    range_path.write_bytes(canonical_json_bytes(range_definition(river_source())))
    assert (
        main(
            [
                "prepare-bounded-river-call-ev-intake",
                "--source",
                str(source),
                "--range",
                str(range_path),
                "--output",
                str(preparation),
                "--intake-id",
                "intake-river-cli",
                "--source-id",
                "fixture-river-cli",
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
    parsed = parse_canonical_model(
        preparation.read_bytes(),
        BoundedRiverCallEvPreparationResultV1,
    )
    assert parsed.status == "ready" and parsed.candidate is not None
    return source, range_path, preparation, parsed


def _confirmation_args(
    preparation: Path,
    confirmation: Path,
    prepared: BoundedRiverCallEvPreparationResultV1,
) -> list[str]:
    assert prepared.candidate is not None
    names = (
        "source",
        "bounded-candidate",
        "source-bindings",
        "focal",
        "extractor",
        "tool-plan",
        "range-definition",
        "range-target",
        "range-binding",
        "equity-model",
        "call-ev-model",
        "candidate",
    )
    arguments = [
        "confirm-bounded-river-call-ev-intake",
        "--preparation",
        str(preparation),
        "--output",
        str(confirmation),
        "--run-id",
        "run-river-cli",
        "--authority-id",
        "local-cli-user",
        "--confirmation-id",
        "confirmation-river-cli",
        "--idempotency-key",
        "idempotency-river-cli",
    ]
    for name, value in zip(names, candidate_hashes(prepared.candidate), strict=True):
        arguments.extend((f"--expected-{name}-sha256", value))
    return arguments


def test_three_stage_river_call_ev_cli_runs_local_product_slice(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source, _range_path, preparation, prepared = _prepare(tmp_path, capsys)
    confirmation = tmp_path / "confirmation.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv(
        "POKER_DELIBERATION_REVISION_RUNS_DIR",
        str(tmp_path / "product"),
    )
    monkeypatch.setenv(
        "POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR",
        str(tmp_path / "budget"),
    )

    assert main([*_confirmation_args(preparation, confirmation, prepared), "--format", "json"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "review-bounded-river-call-ev-confirmed-intake",
                "--source",
                str(source),
                "--preparation",
                str(preparation),
                "--confirmation",
                str(confirmation),
                "--format",
                "summary",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "run-river-cli" in output
    assert "completed" in output


def test_cli_refuses_one_wrong_hash_without_confirmation_output(
    tmp_path: Path,
    capsys,
) -> None:
    _source, _range_path, preparation, prepared = _prepare(tmp_path, capsys)
    confirmation = tmp_path / "confirmation.json"
    arguments = _confirmation_args(preparation, confirmation, prepared)
    index = arguments.index("--expected-call-ev-model-sha256") + 1
    arguments[index] = "0" * 64

    assert main(arguments) == 2
    assert "BRC_E_CONFIRMATION_BINDING" in capsys.readouterr().err
    assert not confirmation.exists()
