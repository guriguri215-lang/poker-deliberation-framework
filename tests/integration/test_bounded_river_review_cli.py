from __future__ import annotations

import json
import shutil
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from poker_deliberation import bounded_river_review_workflow as workflow_product
from poker_deliberation.bounded_river_review_workflow_models import (
    BoundedRiverReviewReportViewV1,
)
from poker_deliberation.cli import main
from poker_deliberation.codex_bridge import product as bridge_product
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    SAFE_INFERENCE_NARRATIVE,
    SAFE_UNKNOWN_NARRATIVE,
    BridgeRole,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore
from poker_deliberation.codex_bridge.transport import DeterministicReadOnlyTransport
from poker_deliberation.reporting import (
    render_bounded_river_review_markdown,
    render_bounded_river_review_summary,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from tests.bounded_river_call_ev_support import range_definition, river_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _StepClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_dir())
    )
    files = {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }
    return directories, files


def _confirmation_cli_options(fields: dict[str, object]) -> list[str]:
    options: list[str] = []
    for name, value in fields.items():
        rendered = "none" if value is None else str(value)
        options.extend((f"--{name.replace('_', '-')}", rendered))
    return options


def test_cli_runs_complete_local_only_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = uuid4().hex[:8]
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wc-{token}"
    storage_root = workflow_root / "s"
    storage_root.mkdir(parents=True)
    source_path = tmp_path / "source.txt"
    range_path = tmp_path / "range.json"
    source = river_source()
    source_path.write_bytes(source)
    range_path.write_bytes(canonical_json_bytes(range_definition(source)))
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(storage_root / "l"))
    monkeypatch.setenv("POKER_DELIBERATION_REVISION_RUNS_DIR", str(storage_root / "p"))
    monkeypatch.setenv("POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR", str(storage_root / "b"))
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    common = [
        "--workflow-root",
        str(workflow_root),
        "--workflow-id",
        "workflow-cli",
        "--repository-root",
        str(REPOSITORY_ROOT),
    ]
    try:
        assert (
            main(
                [
                    "prepare-bounded-river-review",
                    "--source",
                    str(source_path),
                    "--range",
                    str(range_path),
                    *common,
                    "--intake-id",
                    "intake-workflow-cli",
                    "--source-run-id",
                    "run-workflow-cli",
                    "--bridge-run-id",
                    "bridge-workflow-cli",
                    "--source-id",
                    "fixture-workflow-cli",
                    "--source-kind",
                    "repository_fixture",
                    "--license-classification",
                    "repository_owned_mit",
                    "--usage-classification",
                    "redistribution_allowed",
                    "--classification",
                    "public",
                    "--repository-commit",
                    "1" * 40,
                    "--repository-tree",
                    "2" * 40,
                ]
            )
            == 0
        )
        preview = json.loads(capsys.readouterr().out)
        confirmed_at = datetime.now(UTC)
        confirmation_args = [
            "confirm-bounded-river-review",
            *common,
            "--authority-id",
            "local-cli-user",
            "--confirmation-id",
            "confirmation-workflow-cli",
            "--idempotency-key",
            "idempotency-workflow-cli",
            "--expected-plan-sha256",
            preview["plan_sha256"],
            "--confirmed-at",
            confirmed_at.isoformat().replace("+00:00", "Z"),
            "--expires-at",
            (confirmed_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        ]
        for name, value in preview["expected_hashes"].items():
            option_name = name.replace("_sha256", "").replace("_", "-")
            confirmation_args.extend((f"--expected-{option_name}-sha256", value))
        assert main(confirmation_args) == 0
        capsys.readouterr()

        assert main(["status-bounded-river-review", *common]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "ready_to_run"
        assert (
            main(
                [
                    "run-bounded-river-review",
                    "--source",
                    str(source_path),
                    *common,
                ]
            )
            == 0
        )
        completed = json.loads(capsys.readouterr().out)
        assert completed["state"] == "completed_local_only"
        assert completed["completed_roles"] == []

        for command in (
            "status-bounded-river-review",
            "resume-bounded-river-review",
            "replay-bounded-river-review",
        ):
            assert main([command, *common]) == 0
            assert json.loads(capsys.readouterr().out) == completed

        api_key_marker = "private-api-key-marker"
        monkeypatch.setenv("OPENAI_API_KEY", api_key_marker)

        def unexpected_network(*args, **kwargs):
            raise AssertionError("show must not open a network connection")

        def unexpected_write(*args, **kwargs):
            raise AssertionError("show must not invoke a filesystem write primitive")

        monkeypatch.setattr(socket, "create_connection", unexpected_network)
        monkeypatch.setattr(bridge_product.tempfile, "TemporaryDirectory", unexpected_write)
        monkeypatch.setattr(Path, "mkdir", unexpected_write)
        monkeypatch.setattr(Path, "write_bytes", unexpected_write)
        before_show = _tree_snapshot(workflow_root)
        assert main(["show-bounded-river-review", *common, "--format", "json"]) == 0
        report_view_json = capsys.readouterr().out
        report_view_payload = json.loads(report_view_json)
        assert report_view_payload["state"] == "completed_local_only"
        assert report_view_payload["bridge_mode"] == "local_only"
        assert report_view_payload["completed_roles"] == []
        assert report_view_payload["source_run_id"] == "run-workflow-cli"
        assert report_view_payload["bridge_run_id"] == "bridge-workflow-cli"
        assert report_view_payload["report_writer_additive_evidence"] == []
        assert {
            "plan_sha256",
            "confirmation_sha256",
            "linkage_sha256",
            "source_terminal_manifest_sha256",
            "source_terminal_inventory_sha256",
            "bridge_manifest_sha256",
            "bridge_inventory_sha256",
            "final_report_artifact_sha256",
        }.issubset(report_view_payload)
        report_view = BoundedRiverReviewReportViewV1.model_validate_json(report_view_json)
        rendered_outputs = [report_view_json]
        for format_name, renderer in (
            ("summary", render_bounded_river_review_summary),
            ("markdown", render_bounded_river_review_markdown),
        ):
            assert (
                main(
                    [
                        "show-bounded-river-review",
                        *common,
                        "--format",
                        format_name,
                    ]
                )
                == 0
            )
            rendered = capsys.readouterr().out
            assert rendered == renderer(report_view) + "\n"
            rendered_outputs.append(rendered)
        assert _tree_snapshot(workflow_root) == before_show
        assert all(api_key_marker not in output for output in rendered_outputs)
        assert all(str(source_path) not in output for output in rendered_outputs)
        assert all(source.decode("utf-8") not in output for output in rendered_outputs)
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)


def test_cli_runs_five_explicit_supervised_role_cycles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = uuid4().hex[:8]
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wc-supervised-{token}"
    storage_root = workflow_root / "s"
    storage_root.mkdir(parents=True)
    source_path = tmp_path / "source.txt"
    range_path = tmp_path / "range.json"
    source = river_source()
    source_path.write_bytes(source)
    range_path.write_bytes(canonical_json_bytes(range_definition(source)))
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(storage_root / "l"))
    monkeypatch.setenv("POKER_DELIBERATION_REVISION_RUNS_DIR", str(storage_root / "p"))
    monkeypatch.setenv("POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR", str(storage_root / "b"))
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    common = [
        "--workflow-root",
        str(workflow_root),
        "--workflow-id",
        "workflow-cli-supervised",
        "--repository-root",
        str(REPOSITORY_ROOT),
    ]
    runtime_root = workflow_root / "runtime"
    clock = _StepClock()
    transport = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=clock,
    )
    execution_calls: list[BridgeRole] = []

    def execute_deterministically(**kwargs):
        clock.current = max(clock.current, datetime.now(UTC))
        assert kwargs["runtime_root"] == runtime_root
        assert kwargs["codex_binary"] is None
        role = kwargs["role"]
        execution_calls.append(role)
        controller = BoundedCodexBridgeController(
            BoundedCodexBridgeStore(kwargs["bridge_root"]),
            clock=clock,
        )
        source_context = controller.read_source_context(kwargs["bridge_run_id"])
        return controller.execute_confirmed_role(
            kwargs["bridge_run_id"],
            role,
            auth_mode=kwargs["auth_mode"],
            current_source_terminal_manifest_sha256=(
                source_context.source.source_terminal_manifest_sha256
            ),
            transport=transport,
        )

    monkeypatch.setattr(
        workflow_product,
        "execute_product_role",
        execute_deterministically,
    )
    try:
        assert (
            main(
                [
                    "prepare-bounded-river-review",
                    "--source",
                    str(source_path),
                    "--range",
                    str(range_path),
                    *common,
                    "--intake-id",
                    "intake-cli-supervised",
                    "--source-run-id",
                    "run-cli-supervised",
                    "--bridge-run-id",
                    "bridge-cli-supervised",
                    "--source-id",
                    "fixture-cli-supervised",
                    "--source-kind",
                    "repository_fixture",
                    "--license-classification",
                    "repository_owned_mit",
                    "--usage-classification",
                    "redistribution_allowed",
                    "--classification",
                    "public",
                    "--repository-commit",
                    "1" * 40,
                    "--repository-tree",
                    "2" * 40,
                    "--auth-mode",
                    RuntimeAuthModeV1.CODEX_SUBSCRIPTION.value,
                ]
            )
            == 0
        )
        preparation = json.loads(capsys.readouterr().out)
        confirmed_at = datetime.now(UTC)
        confirmation_args = [
            "confirm-bounded-river-review",
            *common,
            "--authority-id",
            "local-cli-user",
            "--confirmation-id",
            "confirmation-cli-supervised",
            "--idempotency-key",
            "idempotency-cli-supervised",
            "--expected-plan-sha256",
            preparation["plan_sha256"],
            "--confirmed-at",
            confirmed_at.isoformat().replace("+00:00", "Z"),
            "--expires-at",
            (confirmed_at + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        ]
        for name, value in preparation["expected_hashes"].items():
            option_name = name.replace("_sha256", "").replace("_", "-")
            confirmation_args.extend((f"--expected-{option_name}-sha256", value))
        assert main(confirmation_args) == 0
        capsys.readouterr()
        assert (
            main(
                [
                    "run-bounded-river-review",
                    "--source",
                    str(source_path),
                    *common,
                ]
            )
            == 0
        )
        status = json.loads(capsys.readouterr().out)
        assert status["state"] == "awaiting_role_review"
        assert status["role_state"] == "awaiting_confirmation"
        assert status["next_role"] == BRIDGE_ROLE_ORDER[0].value
        assert execution_calls == []

        assert main(["show-bounded-river-review", *common, "--format", "json"]) == 0
        initial_report_view = json.loads(capsys.readouterr().out)
        initial_final_report = initial_report_view["final_report"]

        for ordinal, expected_role in enumerate(BRIDGE_ROLE_ORDER):
            assert main(["show-bounded-river-review-role-request", *common]) == 0
            preview_text = capsys.readouterr().out
            preview = json.loads(preview_text)
            assert preview["next_role"] == expected_role.value
            assert preview["next_role_state"] == "awaiting_confirmation"
            assert preview["request"]["role"] == expected_role.value
            assert preview["request"]["provider_fallback_allowed"] is False
            assert preview["request"]["model_fallback_allowed"] is False
            assert source.decode("utf-8") not in preview_text

            confirmation_fields = preview["confirmation_fields"]
            assert confirmation_fields["expected_role"] == expected_role.value
            calls_before_confirmation = tuple(execution_calls)
            assert (
                main(
                    [
                        "confirm-bounded-river-review-role-request",
                        *common,
                        "--authority-id",
                        "local-cli-user",
                        "--confirmation-id",
                        f"confirmation-cli-{expected_role.value}",
                        "--idempotency-key",
                        f"idempotency-cli-{expected_role.value}",
                        *_confirmation_cli_options(confirmation_fields),
                    ]
                )
                == 0
            )
            confirmed = json.loads(capsys.readouterr().out)
            assert tuple(execution_calls) == calls_before_confirmation
            assert confirmed["next_role"] == expected_role.value
            assert confirmed["role_state"] == "executable"
            assert confirmed["next_action"] == "execute_role"

            assert (
                main(
                    [
                        "execute-bounded-river-review-role",
                        *common,
                        "--runtime-root",
                        str(runtime_root),
                    ]
                )
                == 0
            )
            status = json.loads(capsys.readouterr().out)
            assert tuple(execution_calls) == BRIDGE_ROLE_ORDER[: ordinal + 1]
            assert status["completed_roles"] == [
                role.value for role in BRIDGE_ROLE_ORDER[: ordinal + 1]
            ]
            assert status["pending_roles"] == [
                role.value for role in BRIDGE_ROLE_ORDER[ordinal + 1 :]
            ]
            if ordinal + 1 < len(BRIDGE_ROLE_ORDER):
                assert status["next_role"] == BRIDGE_ROLE_ORDER[ordinal + 1].value
                assert status["role_state"] == "awaiting_confirmation"
                assert status["next_action"] == "show_role_request"
            else:
                assert status["state"] == "completed"
                assert status["bridge_status"] == "succeeded"
                assert status["next_role"] is None
                assert status["role_state"] == "terminal"
                assert status["next_action"] == "none"

        assert main(["replay-bounded-river-review", *common]) == 0
        assert json.loads(capsys.readouterr().out) == status
        assert main(["show-bounded-river-review", *common, "--format", "json"]) == 0
        terminal_text = capsys.readouterr().out
        terminal_view = json.loads(terminal_text)
        assert terminal_view["final_report"] == initial_final_report
        assert terminal_view["completed_roles"] == [role.value for role in BRIDGE_ROLE_ORDER]
        assert terminal_view["bridge_status"] == "succeeded"
        assert terminal_view["report_writer_additive_evidence"]
        assert all(
            set(item) == {"conclusion_code", "referenced_evidence_sha256"}
            for item in terminal_view["report_writer_additive_evidence"]
        )
        assert source.decode("utf-8") not in terminal_text
        assert SAFE_INFERENCE_NARRATIVE not in terminal_text
        assert SAFE_UNKNOWN_NARRATIVE not in terminal_text
        assert not runtime_root.exists()
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)


def test_cli_missing_private_source_path_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "private-user-path-marker"
    exit_code = main(
        [
            "prepare-bounded-river-review",
            "--source",
            str(tmp_path / private_marker / "source.txt"),
            "--range",
            str(tmp_path / "range.json"),
            "--workflow-root",
            str(REPOSITORY_ROOT / "tmp" / "unused-workflow"),
            "--workflow-id",
            "workflow-redaction",
            "--intake-id",
            "intake-redaction",
            "--source-run-id",
            "source-run-redaction",
            "--bridge-run-id",
            "bridge-run-redaction",
            "--source-id",
            "source-redaction",
            "--repository-commit",
            "1" * 40,
            "--repository-tree",
            "2" * 40,
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert captured.err.strip() == "error: BRW_E_STORAGE"
    assert private_marker not in captured.err
