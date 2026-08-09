from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.cli import main
from poker_deliberation.codex_bridge import product
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from tests.bounded_river_call_ev_support import admission, app_config
from tests.codex_bridge_support import REPOSITORY_ROOT


def _invoke(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, dict[str, object]]:
    exit_code = main(argv)
    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    assert isinstance(value, dict)
    return exit_code, value


def test_cli_requires_exact_preview_confirmation_and_fails_closed_without_auth(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(product, "verify_bridge_checkout", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(product, "verify_bridge_module_origins", lambda *_args: None)
    temporary_parent = REPOSITORY_ROOT / "tmp"
    temporary_parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="c-", dir=temporary_parent) as raw_root:
        root = Path(raw_root)
        source_config = app_config(root / "p")
        source_run_id = "run-codex-cli-source"
        orchestrator = Orchestrator(config=source_config, provider=LocalProvider())
        orchestrator.run_bounded_river_call_ev_review(admission(run_id=source_run_id))
        monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(source_config.runs_dir))
        monkeypatch.setenv(
            "POKER_DELIBERATION_REVISION_RUNS_DIR",
            str(source_config.revision_runs_dir),
        )
        monkeypatch.setenv(
            "POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR",
            str(source_config.durable_budget_runs_dir),
        )
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        bridge_root = root / "x"
        runtime_root = root / "y"
        common = [
            "--bridge-run-id",
            "bridge-cli-run",
            "--bridge-root",
            str(bridge_root),
            "--repository-root",
            str(REPOSITORY_ROOT),
            "--auth-mode",
            "openai_api",
            "--format",
            "json",
        ]

        code, prepared = _invoke(
            capsys,
            [
                "prepare-bounded-codex-bridge",
                "--source-run-id",
                source_run_id,
                *common,
                "--repository-commit",
                "1" * 40,
                "--repository-tree",
                "2" * 40,
                "--api-max-cost-micro-usd",
                "204000",
            ],
        )
        assert code == 0
        assert prepared["status"] == "approval_required"

        code, preview = _invoke(
            capsys,
            [
                "show-bounded-codex-role-request",
                *common,
                "--role",
                "strategy-analyst",
            ],
        )
        assert code == 0
        outbound = str(preview["outbound_utf8"])
        assert "raw_text" not in outbound
        assert "observations" not in outbound
        assert "final_report" not in outbound.lower()
        assert preview["tool_allowlist"] == []

        code, confirmed = _invoke(
            capsys,
            [
                "confirm-bounded-codex-role-request",
                *common,
                "--role",
                "strategy-analyst",
                "--authority-id",
                "local-cli-user",
                "--confirmation-id",
                "confirmation-cli-strategy",
                "--idempotency-key",
                "idempotency-cli-strategy",
                "--expected-request-sha256",
                str(preview["request_sha256"]),
                "--expected-request-bytes-sha256",
                str(preview["request_bytes_sha256"]),
                "--expected-envelope-sha256",
                str(preview["envelope_sha256"]),
                "--expected-runtime-policy-sha256",
                str(preview["runtime_policy_sha256"]),
                "--expected-runtime-identity",
                str(preview["runtime_identity"]),
                "--expected-model-provider",
                str(preview["model_provider"]),
                "--expected-model",
                str(preview["model"]),
                "--expected-credential-reference",
                str(preview["credential_reference"]),
                "--expected-remote-retention-policy",
                str(preview["remote_retention_policy"]),
            ],
        )
        assert code == 0
        assert confirmed["status"] == "approval_required"

        code, failed = _invoke(
            capsys,
            [
                "execute-bounded-codex-role",
                *common,
                "--runtime-root",
                str(runtime_root),
                "--role",
                "strategy-analyst",
            ],
        )
        assert code == 2
        assert failed["status"] == "failed"
        assert runtime_root.exists() is False

        code, replayed = _invoke(
            capsys,
            ["replay-bounded-codex-bridge", *common],
        )
        assert code == 0
        assert replayed["status"] == "failed"
        assert replayed["completed_roles"] == []
