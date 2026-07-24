"""P2-010B phase revision coordinator adversarial tests."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import ClassificationEvidence
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureCode,
    PhaseRevisionFailureV1,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from tests.integration.test_phase_revision_coordinator import (
    build_valid_scenario,
)

ROOT = Path(__file__).resolve().parents[2]

SECRET_VALUES = (
    "sk-abcdefgh",
    "ghp_abcdefghijklmnopqrst",
    "github_pat_abcdefghijklmnopqrst",
    "AKIAABCDEFGHIJKLMNOP",
    "AIzaabcdefghijklmnopqrst",
    "eyJabcde.abcdefgh.ijklmnop",
    "xoxb-abcdefghij",
    "npm_abcdefghijklmnopqrst",
    "sk_live_abcdefghij",
    "Bearer abcdefgh",
    "token=abcdefghijkl",
    "-----BEGIN PRIVATE KEY-----",
    "access_token=abcdefghijkl",
    "client_secret=abcdefghijkl",
)


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2cs-", dir=parent) as directory:
        yield Path(directory)


def _replace_artifact_bytes(bundle, logical_name: str, data: bytes):  # type: ignore[no-untyped-def]
    artifacts = tuple(
        (
            artifact.model_copy(update={"exact_bytes": data})
            if artifact.logical_name == logical_name
            else artifact
        )
        for artifact in bundle.request.artifacts
    )
    return replace(
        bundle,
        request=bundle.request.model_copy(update={"artifacts": artifacts}),
    )


def _assert_closed_denial(
    *,
    coordinator,
    bundle,
    canary: str,
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
    capsys: pytest.CaptureFixture[str],
) -> None:  # type: ignore[no-untyped-def]
    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.SECRET_DETECTED)
    exposed = (
        repr(result)
        + str(result)
        + result.model_dump_json()
        + caplog.text
        + "\n".join(str(item.message) for item in recwarn)
        + "".join(capsys.readouterr())
    )
    assert canary not in exposed
    assert not isinstance(result, BaseException)
    assert not hasattr(result, "__traceback__")
    assert not (
        coordinator.store.runs_root / bundle.request.run_id / ".revision-store" / "current.json"
    ).exists()


@pytest.mark.parametrize("canary", SECRET_VALUES)
def test_every_secret_value_family_is_closed_and_mutation_zero(
    short_tmp: Path,
    canary: str,
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    input_value = {
        "case_id": "case-secret",
        "kind": "claim",
        "raw_text": f"clean prefix {canary} clean suffix",
    }
    forged = _replace_artifact_bytes(
        bundle,
        "input.json",
        canonical_json_bytes(input_value),
    )

    _assert_closed_denial(
        coordinator=coordinator,
        bundle=forged,
        canary=canary,
        caplog=caplog,
        recwarn=recwarn,
        capsys=capsys,
    )


@pytest.mark.parametrize(
    "key",
    (
        "api_key_note",
        "authorization_hint",
        "bearer_mode",
        "session_cookie",
        "password_hint",
        "passwd_note",
        "private_key_format",
        "client_credential_hint",
    ),
)
def test_sensitive_json_key_families_are_denied(
    short_tmp: Path,
    key: str,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    forged = _replace_artifact_bytes(
        bundle,
        "security_events.json",
        canonical_json_bytes({key: "innocuous"}),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.SECRET_DETECTED)


def test_nested_jsonl_and_markdown_are_scanned_before_canonical_admission(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    jsonl_canary = "gho_abcdefghijklmnopqrst"
    forged_jsonl = _replace_artifact_bytes(
        bundle,
        "evidence.jsonl",
        canonical_json_bytes({"nested": [{"value": jsonl_canary}]}) + b"\n",
    )
    jsonl_result = coordinator.publish(forged_jsonl)
    assert jsonl_result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.SECRET_DETECTED)

    markdown_canary = "xoxp-abcdefghij"
    forged_markdown = _replace_artifact_bytes(
        bundle,
        "final_report.md",
        f"# clean\n\n{markdown_canary}\n".encode(),
    )
    markdown_result = coordinator.publish(forged_markdown)
    assert markdown_result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.SECRET_DETECTED)


def test_tool_result_payload_is_scanned_before_source_graph_admission(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    canary = "sk_test_abcdefghij"
    template = next(
        artifact
        for artifact in bundle.request.artifacts
        if artifact.logical_name == "security_events.json"
    )
    forged_tool_result = template.model_copy(
        update={
            "logical_name": "tool_results/result-secret.json",
            "exact_bytes": canonical_json_bytes(
                {
                    "result_id": "result-secret",
                    "tool_name": "solver_status",
                    "status": "failed",
                    "error": canary,
                }
            ),
        }
    )
    forged = replace(
        bundle,
        request=bundle.request.model_copy(
            update={"artifacts": (*bundle.request.artifacts, forged_tool_result)}
        ),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.SECRET_DETECTED)
    assert canary not in repr(result)


def test_classification_evidence_disagreement_is_persistence_denied(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    target = next(
        artifact
        for artifact in bundle.request.artifacts
        if artifact.logical_name == "final_report.md"
    )
    downgraded = target.model_copy(
        update={
            "classification_evidence": ClassificationEvidence(
                source_classifications=(ContextClassification.SENSITIVE,),
                restricted_secret_check_completed=True,
            )
        }
    )
    forged = replace(
        bundle,
        request=bundle.request.model_copy(
            update={
                "artifacts": tuple(
                    downgraded if artifact is target else artifact
                    for artifact in bundle.request.artifacts
                )
            }
        ),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PERSISTENCE_DENIED)
