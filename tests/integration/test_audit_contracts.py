import json
import tomllib
from pathlib import Path

import yaml

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput, Claim, EpistemicLabel, EvidenceRecord
from poker_deliberation.tools import default_registry

ROOT = Path(__file__).resolve().parents[2]


def test_evidence_is_validated_persisted_and_reported(tmp_path: Path) -> None:
    claim = Claim(claim_id="claim-1", text="rule", label=EpistemicLabel.USER_CLAIM)
    evidence = EvidenceRecord(
        evidence_id="evidence-1",
        source_title="Official rule",
        organization_or_author="Rules body",
        source_type="official",
        identifier="rule-1",
        accessed_date="2026-07-17",
        supported_claim_ids=[claim.claim_id],
        summary="Supports the named claim.",
        source_tier=1,
    )
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    report = orchestrator.run(
        CaseInput(
            kind="claim",
            claims=[claim],
            evidence=[evidence],
            analysis_scope="retrospective",
        )
    )
    assert [item.evidence_id for item in report.evidence] == ["evidence-1"]
    ledger = orchestrator.product_store.read_current(report.run_id).payload_bytes("evidence.jsonl")
    assert json.loads(ledger)["evidence_id"] == "evidence-1"


def test_reproduction_argv_uses_actual_nondefault_run_root(tmp_path: Path) -> None:
    run_root = tmp_path / "run root with spaces"
    report = Orchestrator(AppConfig(runs_dir=run_root)).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={
                "tool_inputs": {
                    "pot_odds": {
                        "pot_before_bet": 100,
                        "opponent_bet": 50,
                        "call_cost": 50,
                    }
                }
            },
        )
    )
    argv = json.loads(report.reproduction_steps[0].removeprefix("argv-json: "))
    input_path = Path(argv[-1])
    assert input_path.is_file()
    reproduced = default_registry().execute(argv[2], json.loads(input_path.read_text("utf-8")))
    assert reproduced.output == report.tool_results[0].output


def test_manifest_and_registry_names_match() -> None:
    manifest = yaml.safe_load((ROOT / "tools" / "manifest.yaml").read_text(encoding="utf-8"))
    manifest_names = sorted(item["name"] for item in manifest["tools"])
    assert manifest_names == default_registry().names()


def test_build_requirements_are_pinned_in_lock() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    locked = {
        line.split("==", 1)[0].lower(): line.split("==", 1)[1]
        for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if "==" in line and not line.startswith("#")
    }
    for requirement in project["build-system"]["requires"]:
        name, version = requirement.split("==", 1)
        assert locked[name.lower()] == version
