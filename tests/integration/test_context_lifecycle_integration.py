import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import context_payload
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import ProviderAvailability, ProviderControl, ProviderStatus
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    AgentReport,
    CaseInput,
    ConfidenceGrade,
)

FIXED_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


class CapturingProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[AgentContext, AgentAssignment]] = []

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="capturing-test",
            reason="integration test provider",
            version="1.0.0",
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        control.raise_if_cancelled()
        self.calls.append((context, assignment))
        return AgentReport(
            agent_role=assignment.agent_role,
            task=assignment.task,
            confidence=ConfidenceGrade.C,
        )


def test_orchestrator_delivers_exact_allowlisted_fresh_contexts(tmp_path: Path) -> None:
    provider = CapturingProvider()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: FIXED_NOW,
    ).run(
        CaseInput(
            kind="strategy",
            raw_text="compare the lines",
            analysis_scope="retrospective",
        ),
        run_id="run-context-integration",
    )

    assert report.run_status == "completed"
    assert len(provider.calls) == 4
    assert len({id(context) for context, _ in provider.calls}) == 4
    for context, assignment in provider.calls:
        assert assignment.context_keys == sorted(context_payload(context))
        assert all("." not in name for name in assignment.context_keys)

    assert len(report.agent_execution_records) == 4
    assert len({record.context_id for record in report.agent_execution_records}) == 4
    assert len({record.context_attempt_id for record in report.agent_execution_records}) == 4
    for (context, _assignment), record in zip(
        provider.calls,
        report.agent_execution_records,
        strict=True,
    ):
        assert record.status.value == "completed"
        assert record.context_schema_version == "1.0.0"
        assert record.context_classification == "internal"
        legacy_payload = json.dumps(
            context.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert record.context_sha256 == hashlib.sha256(legacy_payload.encode("utf-8")).hexdigest()
        assert record.context_sha256 != record.context_payload_sha256
        assert record.context_source_sha256 == record.context_payload_sha256
        assert record.context_policy_sha256 is not None
        assert record.context_envelope_sha256 is not None
        assert record.context_expires_at is not None
        assert record.context_producer_runtime == "python-local"
        assert record.context_consumer_runtime == "python-local"


def test_context_payload_is_not_persisted_as_a_new_lifecycle_artifact(tmp_path: Path) -> None:
    provider = CapturingProvider()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: FIXED_NOW,
    ).run(CaseInput(kind="strategy", raw_text="artifact canary", analysis_scope="retrospective"))
    run_dir = tmp_path / "runs" / report.run_id
    names = {path.name for path in run_dir.rglob("*") if path.is_file()}

    assert "context_envelope.json" not in names
    assert "context_policy.json" not in names
    records = json.loads((run_dir / "agent_execution_records.json").read_text(encoding="utf-8"))
    assert records
    assert all("canonical_payload" not in record for record in records)


def test_local_provider_behavior_remains_non_generative_and_completed(tmp_path: Path) -> None:
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        context_clock=lambda: FIXED_NOW,
    ).run(CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"))

    assert report.run_status == "completed"
    assert report.agent_execution_records
    assert {
        (record.provider, record.status.value) for record in report.agent_execution_records
    } == {("local", "completed")}
    report_files = list((tmp_path / "runs" / report.run_id / "agent_reports").glob("*.json"))
    assert report_files
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["conclusions"] == [] for path in report_files
    )


def test_calculation_case_keeps_provider_uninvoked_and_assignment_artifact_valid(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: FIXED_NOW,
    ).run(
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

    assert report.run_status == "completed"
    assert provider.calls == []
    assert report.agent_execution_records == []
    assignments = json.loads(
        (tmp_path / "runs" / report.run_id / "assignments.json").read_text(encoding="utf-8")
    )
    assert all(assignment["context_keys"] == [] for assignment in assignments)


def test_legacy_execution_record_without_lifecycle_metadata_still_validates() -> None:
    from poker_deliberation.schemas import AgentExecutionRecord

    record = AgentExecutionRecord.model_validate(
        {
            "assignment_id": "assignment-legacy",
            "agent_role": "skeptic",
            "provider": "local",
            "context_sha256": "0" * 64,
            "status": "completed",
            "started_at": FIXED_NOW,
            "completed_at": FIXED_NOW,
        }
    )

    assert record.context_id is None
    assert record.context_payload_sha256 is None
    assert record.context_envelope_sha256 is None
