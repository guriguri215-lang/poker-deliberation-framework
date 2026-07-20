import json
from pathlib import Path

from poker_deliberation.cli import main

ROOT = Path(__file__).resolve().parents[2]


def test_doctor_without_api_key(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = main(["doctor", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "ok"
    assert payload["external_solver"] == "unavailable"
    assert payload["providers"]["local"]["status"] == "available"
    assert payload["providers"]["openai_agents"]["status"] == "disabled"
    assert payload["providers"]["openai_agents"]["available"] is False


def test_doctor_markdown_is_fenced_json(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = main(["doctor", "--format", "markdown"])
    rendered = capsys.readouterr().out
    assert exit_code == 0
    assert rendered.startswith("```json\n")
    assert rendered.endswith("```\n")
    payload = json.loads(rendered.removeprefix("```json\n").removesuffix("```\n"))
    assert payload["status"] == "ok"


def test_list_tools_json_and_markdown_have_twenty_canonical_tools(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["list-tools", "--format", "json"]) == 0
    json_payload = json.loads(capsys.readouterr().out)
    assert len(json_payload) == 20

    assert main(["list-tools", "--format", "markdown"]) == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("```json\n")
    markdown_payload = json.loads(rendered.removeprefix("```json\n").removesuffix("```\n"))
    assert markdown_payload == json_payload


def test_calculate_cli(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = main(
        [
            "calculate",
            "pot_odds",
            "--analysis-scope",
            "retrospective",
            "--input",
            str(ROOT / "examples" / "pot_odds_input.json"),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["output"]["required_equity"] == 0.25
    assert payload["exactness"] == "exact"
    assert payload["numeric_exactness"] == "floating-verified"
    assert payload["contract_version"] == "2.0.0"
    assert payload["verification"]["passed"] is True


def test_calculate_cli_markdown_has_v2_json_parity_fields(capsys) -> None:  # type: ignore[no-untyped-def]
    exit_code = main(
        [
            "calculate",
            "pot_odds",
            "--analysis-scope",
            "retrospective",
            "--input",
            str(ROOT / "examples" / "pot_odds_input.json"),
            "--format",
            "markdown",
        ]
    )
    rendered = capsys.readouterr().out
    assert exit_code == 0
    assert "- 数値区分: `floating-verified`" in rendered
    assert '- 契約バージョン: `"2.0.0"`' in rendered
    assert '"passed": true' in rendered
    assert '"required_equity": 0.25' in rendered


def test_cli_rejects_non_finite_json_numbers(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    input_path = tmp_path / "nan-matrix.json"
    input_path.write_text('{"matrix": [[1, -1], [-1, 1]], "tolerance": NaN}', encoding="utf-8")
    exit_code = main(
        [
            "calculate",
            "matrix_game",
            "--analysis-scope",
            "retrospective",
            "--input",
            str(input_path),
            "--format",
            "json",
        ]
    )
    assert exit_code == 2
    assert "non-finite JSON number" in capsys.readouterr().err


def test_calculate_cli_requires_explicit_retrospective_scope(capsys) -> None:  # type: ignore[no-untyped-def]
    args = [
        "calculate",
        "pot_odds",
        "--input",
        str(ROOT / "examples" / "pot_odds_input.json"),
        "--format",
        "json",
    ]
    assert main(args) == 2
    assert "retrospective-only" in capsys.readouterr().err

    args[2:2] = ["--analysis-scope", "real_time"]
    assert main(args) == 2
    assert "retrospective-only" in capsys.readouterr().err


def test_review_hand_and_show_cli(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "runs"))
    exit_code = main(
        [
            "review-hand",
            "--file",
            str(ROOT / "examples" / "valid_hand.json"),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["tool_results"][0]["tool_name"] == "hand_validator"
    assert report["agent_execution_records"]
    assert all(
        record["context_schema_version"] == "1.0.0"
        and record["context_envelope_sha256"]
        and record["context_payload_sha256"] == record["context_source_sha256"]
        for record in report["agent_execution_records"]
    )
    run_id = report["run_id"]
    assert main(["show", run_id, "--format", "json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["run_id"] == run_id
    assert shown["agent_execution_records"] == report["agent_execution_records"]


def test_review_hand_normalizes_documented_free_text(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "runs"))
    exit_code = main(
        [
            "review-hand",
            "--file",
            str(ROOT / "examples" / "free_text_hand.txt"),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["reconstructed_input"]["hand"]["players"][0]["player_id"] == "hero"
    assert report["tool_results"][0]["output"]["valid"] is True


def test_approval_required_cli_uses_distinct_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    case_path = tmp_path / "approval-case.json"
    case_path.write_text(
        json.dumps(
            {
                "kind": "strategy",
                "raw_text": "external solver",
                "metadata": {
                    "approval_requests": [
                        {
                            "approval_id": "approval-cli",
                            "requested_action": "external solver",
                            "reason": "test approval UX",
                            "expected_benefit": "solver output",
                            "risks": ["external execution"],
                            "cost_or_resource_estimate": "unknown",
                            "alternatives": ["local sensitivity"],
                            "effect_of_declining": "no solver result",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "runs"))
    exit_code = main(["review-strategy", "--file", str(case_path), "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert report["run_status"] == "approval_required"
    assert report["approvals"][0]["status"] == "pending"
    resume_exit_code = main(
        [
            "resume",
            report["run_id"],
            "--approve",
            report["approvals"][0]["approval_id"],
            "--format",
            "json",
        ]
    )
    resumed = json.loads(capsys.readouterr().out)
    assert resume_exit_code == 2
    assert resumed["run_status"] == "failed_with_limitations"
    assert resumed["agent_execution_records"] == report["agent_execution_records"]
