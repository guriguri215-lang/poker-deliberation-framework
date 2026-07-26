import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.budgets import ExecutionClass
from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import (
    ContextEnvelope,
    ContextHandoffRefused,
    ContextLifecycleError,
    ContextPolicy,
    build_context_envelope,
    build_retry_context_envelope,
    context_payload,
    validate_context_envelope,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import ProviderAvailability, ProviderControl, ProviderStatus
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    AgentReport,
    CaseInput,
    Claim,
    EpistemicLabel,
)

pytestmark = pytest.mark.adversarial

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _context(text: str = "review") -> AgentContext:
    return AgentContext(
        kind="claim",
        objective="verify",
        claims=[Claim(text=text, label=EpistemicLabel.USER_CLAIM)],
    )


def _assignment(context: AgentContext) -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-security",
        agent_role="skeptic",
        task="challenge",
        context_keys=sorted(context_payload(context)),
    )


def _envelope(text: str = "review") -> tuple[ContextEnvelope, AgentAssignment]:
    context = _context(text)
    assignment = _assignment(context)
    envelope = build_context_envelope(
        context,
        assignment,
        run_id="run-security",
        expires_at=NOW + timedelta(seconds=10),
        clock=lambda: NOW,
        context_id="context-security",
        attempt_id="attempt-security",
    )
    return envelope, assignment


def test_extra_fields_unknown_versions_and_dotted_allowlists_fail_closed() -> None:
    envelope, _ = _envelope()
    extra = envelope.model_dump(mode="python")
    extra["surprise"] = True
    with pytest.raises(ValidationError):
        ContextEnvelope.model_validate(extra)

    unknown_canonicalization = envelope.model_dump(mode="python")
    unknown_canonicalization["canonicalization_version"] = "unknown"
    with pytest.raises(ValidationError):
        ContextEnvelope.model_validate(unknown_canonicalization)

    with pytest.raises(ValidationError):
        ContextPolicy(
            expires_at=NOW + timedelta(seconds=1),
            allowed_fields=("claims.0.text",),
        )


def test_nested_payload_tampering_and_replay_correlations_are_rejected() -> None:
    envelope, assignment = _envelope()
    tampered = envelope.model_copy(
        update={"canonical_payload": envelope.canonical_payload.replace("review", "forged")}
    )
    with pytest.raises(ContextLifecycleError, match="payload integrity"):
        validate_context_envelope(
            tampered,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
        )

    for run_id, attempt_id in (
        ("run-other", "attempt-security"),
        ("run-security", "attempt-other"),
    ):
        with pytest.raises(ContextLifecycleError, match="correlation"):
            validate_context_envelope(
                envelope,
                assignment,
                run_id=run_id,
                expected_context_id="context-security",
                attempt_id=attempt_id,
                now=NOW,
            )
    other_assignment = assignment.model_copy(update={"assignment_id": "assignment-other"})
    with pytest.raises(ContextLifecycleError, match="assignment correlation"):
        validate_context_envelope(
            envelope,
            other_assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
        )
    with pytest.raises(ContextLifecycleError, match="context ID correlation"):
        validate_context_envelope(
            envelope,
            assignment,
            run_id="run-security",
            expected_context_id="context-other",
            attempt_id="attempt-security",
            now=NOW,
        )


def test_forged_parent_source_and_expired_context_are_rejected() -> None:
    envelope, assignment = _envelope()

    with pytest.raises(ContextLifecycleError, match="parent lineage"):
        validate_context_envelope(
            envelope,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
            expected_parent_context_id="context-parent",
        )
    forged_source = envelope.model_copy(
        update={"lineage": envelope.lineage.model_copy(update={"source_sha256": "f" * 64})}
    )
    with pytest.raises(ContextLifecycleError, match="source hash"):
        validate_context_envelope(
            forged_source,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
        )
    with pytest.raises(ContextLifecycleError, match="expired"):
        validate_context_envelope(
            envelope,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=envelope.policy.expires_at,
        )


def test_retry_rejects_cross_run_cross_assignment_and_invalid_parent() -> None:
    parent, assignment = _envelope()
    context = _context()

    with pytest.raises(ContextLifecycleError, match="retry run"):
        build_retry_context_envelope(
            parent,
            context,
            assignment,
            run_id="run-other",
            expires_at=NOW + timedelta(seconds=20),
            clock=lambda: NOW + timedelta(seconds=10),
        )
    other_assignment = assignment.model_copy(update={"assignment_id": "assignment-other"})
    with pytest.raises(ContextLifecycleError, match="retry assignment"):
        build_retry_context_envelope(
            parent,
            context,
            other_assignment,
            run_id="run-security",
            expires_at=NOW + timedelta(seconds=20),
            clock=lambda: NOW + timedelta(seconds=10),
        )
    invalid_parent = parent.model_copy(update={"canonical_payload": "{}"})
    with pytest.raises(ContextLifecycleError, match="payload integrity"):
        build_retry_context_envelope(
            invalid_parent,
            context,
            assignment,
            run_id="run-security",
            expires_at=NOW + timedelta(seconds=20),
            clock=lambda: NOW + timedelta(seconds=5),
        )

    with pytest.raises(ContextLifecycleError, match="expired"):
        build_retry_context_envelope(
            parent,
            context,
            assignment,
            run_id="run-security",
            expires_at=NOW + timedelta(seconds=20),
            clock=lambda: parent.policy.expires_at,
        )
    with pytest.raises(ContextLifecycleError, match="created in the future"):
        build_retry_context_envelope(
            parent,
            context,
            assignment,
            run_id="run-security",
            expires_at=NOW + timedelta(seconds=20),
            clock=lambda: NOW - timedelta(seconds=1),
        )


def test_retry_requires_the_parent_root_source_even_after_hash_recalculation() -> None:
    parent, assignment = _envelope()
    retry = build_retry_context_envelope(
        parent,
        _context(),
        assignment,
        run_id="run-security",
        expires_at=NOW + timedelta(seconds=20),
        clock=lambda: NOW + timedelta(seconds=5),
        context_id="context-retry",
        attempt_id="attempt-retry",
    )
    forged_payload = retry.model_dump(mode="json")
    forged_payload["lineage"]["source_sha256"] = "f" * 64
    forged_payload.pop("integrity_sha256")
    canonical = json.dumps(
        forged_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    forged = ContextEnvelope.model_validate_json(
        json.dumps(
            {
                **forged_payload,
                "integrity_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        )
    )

    with pytest.raises(ContextLifecycleError, match="source lineage"):
        validate_context_envelope(
            forged,
            assignment,
            run_id="run-security",
            expected_context_id="context-retry",
            attempt_id="attempt-retry",
            now=NOW + timedelta(seconds=5),
            expected_parent_context_id=parent.lineage.context_id,
            expected_source_sha256=parent.lineage.source_sha256,
        )
    with pytest.raises(ContextLifecycleError, match="requires expected source"):
        validate_context_envelope(
            retry,
            assignment,
            run_id="run-security",
            expected_context_id="context-retry",
            attempt_id="attempt-retry",
            now=NOW + timedelta(seconds=5),
            expected_parent_context_id=parent.lineage.context_id,
        )


@pytest.mark.parametrize(
    "credential_key",
    ["refresh_token", "id_token", "private_key", "client_secret", "credential"],
)
def test_nested_structured_credentials_never_reach_provider(
    credential_key: str,
) -> None:
    context = AgentContext(
        kind="calculation",
        objective="verify",
        tool_inputs={"payload": {credential_key: "structured-canary"}},
    )
    assignment = _assignment(context)
    envelope = build_context_envelope(
        context,
        assignment,
        run_id="run-security",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
        context_id="context-security",
        attempt_id="attempt-security",
    )
    provider = BoundaryProvider()

    assert envelope.policy.classification.value == "restricted"
    with pytest.raises(ContextHandoffRefused):
        provider_context = validate_context_envelope(
            envelope,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
        )
        provider.analyze(provider_context, assignment, ProviderControl(timeout_seconds=1))
    assert provider.call_count == 0


def test_detected_credential_is_restricted_and_never_materialized() -> None:
    envelope, assignment = _envelope("api_key=sk-supersecret123456")

    assert envelope.policy.classification.value == "restricted"
    with pytest.raises(ContextHandoffRefused, match="restricted"):
        validate_context_envelope(
            envelope,
            assignment,
            run_id="run-security",
            expected_context_id="context-security",
            attempt_id="attempt-security",
            now=NOW,
        )


class BoundaryProvider:
    def __init__(self, *, available: bool = True, mismatch: bool = False) -> None:
        self.available = available
        self.mismatch = mismatch
        self.call_count = 0

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=(ProviderStatus.AVAILABLE if self.available else ProviderStatus.UNAVAILABLE),
            available=self.available,
            provider="boundary-test",
            reason="test boundary",
            execution_class=ExecutionClass.LOCAL_FREE,
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        self.call_count += 1
        return AgentReport(
            agent_role=("wrong-role" if self.mismatch else assignment.agent_role),
            task=("wrong-task" if self.mismatch else assignment.task),
        )


def test_restricted_case_calls_provider_zero_times_and_leaks_no_secret(tmp_path: Path) -> None:
    canary = "sk-providerboundary123456"
    provider = BoundaryProvider()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: NOW,
    ).run(
        CaseInput(
            kind="claim",
            claims=[
                Claim(
                    text=f"api_key={canary}",
                    label=EpistemicLabel.USER_CLAIM,
                )
            ],
            analysis_scope="retrospective",
        )
    )

    assert provider.call_count == 0
    assert {record.status.value for record in report.agent_execution_records} == {"refused"}
    run_dir = tmp_path / "runs" / report.run_id
    combined = (
        report.model_dump_json()
        + "\n"
        + "\n".join(
            path.read_text(encoding="utf-8") for path in run_dir.rglob("*") if path.is_file()
        )
    )
    assert canary not in combined


def test_unavailable_provider_calls_analyze_zero_times(tmp_path: Path) -> None:
    provider = BoundaryProvider(available=False)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: NOW,
    ).run(CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"))

    assert provider.call_count == 0
    assert {record.status.value for record in report.agent_execution_records} == {"refused"}


def test_provider_report_role_or_task_mismatch_is_not_accepted(tmp_path: Path) -> None:
    provider = BoundaryProvider(mismatch=True)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=lambda: NOW,
    ).run(CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"))

    assert provider.call_count == 4
    assert {record.status.value for record in report.agent_execution_records} == {"failed"}
    assert sum("provider report correlation mismatch" in item for item in report.data_quality) == 4
    assert all(
        "wrong-role" not in section and "wrong-task" not in section
        for section in report.analysis_sections
    )
