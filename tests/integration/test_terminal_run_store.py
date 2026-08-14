from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import poker_deliberation.storage.terminal_store as terminal_store_module
from poker_deliberation.budgets.contracts import BudgetPolicyV2
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.schemas import CaseInput, FinalReport
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    run_id_sha256,
)
from poker_deliberation.storage.terminal_canonical import (
    inventory_entry,
    lifecycle_audit_sha256,
    product_payload_commitments,
)
from poker_deliberation.storage.terminal_models import (
    BudgetSettlementBindingV2,
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
    VerifiedPayloadV2,
)
from poker_deliberation.storage.terminal_store import (
    DurableBudgetCoordinator,
    TerminalPublishRequest,
    TerminalRunStore,
    provisional_budget_binding,
)

NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2t-", dir=parent) as directory:
        yield Path(directory)


class FakeBudget:
    def __init__(self, *, settle_visible: bool = True) -> None:
        self.calls: list[str] = []
        self.settle_visible = settle_visible
        self.settled: set[str] = set()

    def reserve(
        self,
        request: TerminalPublishRequest,
        *,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None:
        assert artifact_bytes > 0
        assert run_bytes >= artifact_bytes
        self.calls.append(f"reserve:{request.transaction_id}")

    def settle(
        self,
        request: TerminalPublishRequest,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None:
        assert len(pointer_sha256) == len(effect_evidence_sha256) == 64
        assert run_bytes >= artifact_bytes
        self.calls.append(f"settle:{request.transaction_id}")
        if self.settle_visible:
            self.settled.add(request.budget_binding.settlement_id)

    def release_no_effect(
        self,
        request: TerminalPublishRequest,
        *,
        evidence_sha256: str,
    ) -> None:
        assert len(evidence_sha256) == 64
        self.calls.append(f"release:{request.transaction_id}")

    def verify(
        self,
        run_id: str,
        binding: BudgetSettlementBindingV2,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
    ) -> bool:
        del run_id, pointer_sha256, effect_evidence_sha256
        return binding.settlement_id in self.settled


def _binding(run_id: str, suffix: str) -> BudgetSettlementBindingV2:
    return BudgetSettlementBindingV2(
        budget_run_id_sha256=run_id_sha256(run_id),
        budget_policy_sha256="1" * 64,
        reservation_operation_id=f"reserve-{suffix}",
        reservation_request_sha256="2" * 64,
        permit_id=f"permit-{suffix}",
        settlement_operation_id=f"settle-{suffix}",
        settlement_id=f"settlement-{suffix}",
    )


def _payload(
    logical_name: str,
    data: bytes,
    *,
    schema: str,
) -> VerifiedPayloadV2:
    return VerifiedPayloadV2(
        inventory=inventory_entry(
            logical_name=logical_name,
            data=data,
            media_type="application/json",
            artifact_schema_version=schema,
            serialization="poker-run-storage-json-v1",
        ),
        exact_bytes=data,
    )


def _request(
    run_id: str,
    *,
    transaction_suffix: str,
    publication_kind: str,
    status: str,
    expected: tuple[int, str, str] | None = None,
) -> TerminalPublishRequest:
    transaction_id = "txn-" + transaction_suffix * 32
    report_status = {
        "approval_required": "approval_required",
        "succeeded": "completed",
        "failed": "failed_with_limitations",
    }.get(status, "failed_with_limitations")
    report = FinalReport(
        run_id=run_id,
        run_status=report_status,
        conclusion=f"{status} fixture",
    )
    event_targets = {
        "approval_required": [
            "NORMALIZE",
            "DATA_VALIDATION",
            "HUMAN_REVIEW_REQUIRED",
        ],
        "succeeded": [
            "NORMALIZE",
            "DATA_VALIDATION",
            "TASK_ROUTING",
            "TOOL_AND_RESEARCH",
            "CRITIQUE",
            "ADJUDICATION",
            "FINAL_SYNTHESIS",
            "COMPLETED",
        ],
        "failed": ["FAILED_WITH_LIMITATIONS"],
    }.get(status, ["FAILED_WITH_LIMITATIONS"])
    event_source = "INTAKE"
    events: list[dict[str, str]] = []
    for event_target in event_targets:
        events.append(
            {
                "source": event_source,
                "target": event_target,
                "reason": "terminal store fixture",
            }
        )
        event_source = event_target
    state = canonical_json_bytes(
        {
            "state": {
                "approval_required": "HUMAN_REVIEW_REQUIRED",
                "succeeded": "COMPLETED",
                "failed": "FAILED_WITH_LIMITATIONS",
            }.get(status, "FAILED_WITH_LIMITATIONS"),
            "events": events,
            "deliberation_rounds": 0,
            "tool_retries": {},
            "elapsed_seconds": 0.0,
        }
    )
    input_bytes = canonical_json_bytes(
        CaseInput(
            kind="calculation",
            raw_text="fixture",
            analysis_scope="retrospective",
        )
    )
    payloads = [
        _payload(
            "input.json",
            input_bytes,
            schema="poker-case-input-artifact-v1",
        ),
        _payload(
            "state.json",
            state,
            schema="poker-workflow-state-artifact-v1",
        ),
        _payload(
            "final_report.json",
            canonical_json_bytes(report),
            schema="poker-final-report-artifact-v2",
        ),
    ]
    lifecycle_digest = None
    if publication_kind == "product_terminal":
        lifecycle = canonical_json_bytes([])
        payloads.append(
            _payload(
                "lifecycle_audit.json",
                lifecycle,
                schema="poker-lifecycle-audit-list-artifact-v1",
            )
        )
        lifecycle_digest = lifecycle_audit_sha256(lifecycle)
    commitments = product_payload_commitments(
        {payload.inventory.logical_name: payload.exact_bytes for payload in payloads},
        run_id=run_id,
        status=status,
    )
    expected_revision, expected_manifest, expected_pointer = (
        (None, None, None) if expected is None else expected
    )
    return TerminalPublishRequest(
        run_id=run_id,
        transaction_id=transaction_id,
        publication_kind=publication_kind,
        status=status,
        proposed_revision=(1 if expected_revision is None else expected_revision + 1),
        expected_revision=expected_revision,
        expected_manifest_sha256=expected_manifest,
        expected_pointer_sha256=expected_pointer,
        created_at=NOW,
        updated_at=NOW,
        published_at=NOW,
        framework_version="0.1.0",
        source_commit_id="3" * 64,
        tool_contract_versions=(),
        canonical_input_sha256=commitments[0],
        config_sha256="5" * 64,
        budget_binding=_binding(run_id, transaction_suffix),
        redaction_policy_sha256="6" * 64,
        state_checkpoint_sha256=commitments[1],
        event_head_sha256=commitments[2],
        approval_lineage_head_sha256=commitments[3],
        context_lineage_head_sha256=commitments[4],
        execution_lineage_head_sha256=commitments[5],
        legacy_source=None,
        lifecycle_audit_sha256=lifecycle_digest,
        payloads=tuple(payloads),
    )


def _store(
    tmp_path: Path,
    budget: FakeBudget,
    *,
    fault_injector=None,
) -> TerminalRunStore:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    store = TerminalRunStore(
        tmp_path / "product",
        legacy,
        budget=budget,
        fault_injector=fault_injector,
        source_commit_id="3" * 64,
    )
    store.initialize(initialized_at=NOW)
    return store


def test_terminal_publish_is_marker_last_then_cas_then_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks: list[str] = []
    budget = FakeBudget()
    store = _store(tmp_path, budget, fault_injector=hooks.append)
    request = _request(
        "run-1",
        transaction_suffix="a",
        publication_kind="product_terminal",
        status="succeeded",
    )
    semantic_verification_calls = 0
    original_product_payload_commitments = terminal_store_module.product_payload_commitments

    def counted_product_payload_commitments(*args: object, **kwargs: object):
        nonlocal semantic_verification_calls
        semantic_verification_calls += 1
        return original_product_payload_commitments(*args, **kwargs)

    monkeypatch.setattr(
        terminal_store_module,
        "product_payload_commitments",
        counted_product_payload_commitments,
    )

    outcome = store.publish(request)
    # One call is request preparation and one is the mandatory post-settlement
    # public read. The locked pre-pointer revision read must not add a third
    # semantic replay of the same exact payload.
    assert semantic_verification_calls == 2
    read = store.read_current("run-1")
    assert semantic_verification_calls == 3
    with pytest.raises(ValueError, match="unknown staged revision integrity capability"):
        store._read_revision(
            request.run_id,
            read.pointer,
            verify_budget=False,
            staged_revision_integrity_capability=object(),
        )
    with pytest.raises(ValueError, match="cannot bypass a budget-verified read"):
        store._read_revision(
            request.run_id,
            read.pointer,
            verify_budget=True,
            staged_revision_integrity_capability=(
                terminal_store_module._STAGED_REVISION_INTEGRITY_ONLY
            ),
        )

    assert outcome.outcome_kind == "published"
    assert read.read_status is RunReadStatus.SUCCEEDED
    assert read.resume_eligible is False
    assert (
        FinalReport.model_validate_json(read.payload_bytes("final_report.json")).run_status
        == "completed"
    )
    assert hooks.index("completion.after_reread") < hooks.index("revision.before_rename")
    assert hooks.index("revision.after_rename") < hooks.index("current.before_replace")
    assert budget.calls == [
        f"reserve:{request.transaction_id}",
        f"settle:{request.transaction_id}",
    ]


def test_product_commitments_reject_orphan_tool_input_payload() -> None:
    request = _request(
        "run-orphan-tool-input",
        transaction_suffix="f",
        publication_kind="product_terminal",
        status="succeeded",
    )
    payloads = {payload.inventory.logical_name: payload.exact_bytes for payload in request.payloads}
    payloads["tool_results/tool-result-orphan.input.json"] = b"{}"

    with pytest.raises(
        CanonicalStorageError,
        match="tool input/result inventory pairing mismatch",
    ):
        product_payload_commitments(
            payloads,
            run_id=request.run_id,
            status=request.status,
        )


def test_checkpoint_can_advance_once_by_exact_pointer_cas(tmp_path: Path) -> None:
    budget = FakeBudget()
    store = _store(tmp_path, budget)
    checkpoint = _request(
        "run-2",
        transaction_suffix="b",
        publication_kind="product_checkpoint",
        status="approval_required",
    )
    store.publish(checkpoint)
    first = store.read_current("run-2")
    terminal = _request(
        "run-2",
        transaction_suffix="c",
        publication_kind="product_terminal",
        status="failed",
        expected=(
            first.revision,
            first.manifest_sha256,
            first.current_pointer_sha256,
        ),
    )

    store.publish(terminal)
    second = store.read_current("run-2")

    assert second.read_status is RunReadStatus.FAILED
    assert second.reachable_revisions == (2, 1)
    stale = replace(terminal, transaction_id="txn-" + "d" * 32)
    with pytest.raises(ProductRunError) as captured:
        store.publish(stale)
    assert captured.value.failure.code is ProductRunFailureCode.RUN_CONFLICT
    assert store.read_current("run-2").current_pointer_sha256 == (second.current_pointer_sha256)


def test_reader_withholds_terminal_status_until_exact_settlement(tmp_path: Path) -> None:
    budget = FakeBudget(settle_visible=False)
    store = _store(tmp_path, budget)
    request = _request(
        "run-3",
        transaction_suffix="e",
        publication_kind="product_terminal",
        status="succeeded",
    )

    with pytest.raises(ProductRunError) as captured:
        store.publish(request)

    assert captured.value.failure.code is ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED
    assert captured.value.failure.read_status is RunReadStatus.INCOMPLETE
    with pytest.raises(ProductRunError) as reread:
        store.read_current("run-3")
    assert reread.value.failure.read_status is RunReadStatus.INCOMPLETE


def test_payload_tamper_is_corrupt_not_completed(tmp_path: Path) -> None:
    budget = FakeBudget()
    store = _store(tmp_path, budget)
    request = _request(
        "run-4",
        transaction_suffix="f",
        publication_kind="product_terminal",
        status="succeeded",
    )
    store.publish(request)
    read = store.read_current("run-4")
    report_path = store.report_path(read, "json")
    data = report_path.read_bytes()
    report_path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))

    with pytest.raises(ProductRunError) as captured:
        store.read_current("run-4")

    assert captured.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert captured.value.failure.read_status is RunReadStatus.CORRUPT


def test_pre_pointer_integrity_read_rejects_payload_byte_tamper(
    tmp_path: Path,
) -> None:
    budget = FakeBudget()
    store: TerminalRunStore
    request: TerminalPublishRequest

    def corrupt_after_revision_rename(hook: str) -> None:
        if hook != "revision.after_rename":
            return
        report_path = (
            store.runs_root
            / request.run_id
            / ".terminal-store"
            / "revisions"
            / f"r1-{request.transaction_id}"
            / "payload"
            / "final_report.json"
        )
        data = report_path.read_bytes()
        report_path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))

    store = _store(tmp_path, budget, fault_injector=corrupt_after_revision_rename)
    request = _request(
        "run-pre-pointer-tamper",
        transaction_suffix="7",
        publication_kind="product_terminal",
        status="succeeded",
    )

    with pytest.raises(ProductRunError) as captured:
        store.publish(request)

    assert captured.value.failure.code is ProductRunFailureCode.DURABILITY_UNCONFIRMED
    current = store.runs_root / request.run_id / ".terminal-store" / "current.json"
    assert not current.exists()


def test_real_durable_budget_binding_reserves_and_settles_exact_pointer(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    budget_root = short_tmp / "budget"
    initialize_durable_budget_root(
        budget_root,
        legacy,
        root_id="root-" + "1" * 32,
        initialized_at=NOW,
    )
    policy = BudgetPolicyV2(max_runtime_seconds=30.0)
    durable = DurableBudgetStore(
        budget_root,
        legacy,
        wall_clock=lambda: NOW,
    )
    coordinator = DurableBudgetCoordinator(durable, policy)
    store = TerminalRunStore(
        short_tmp / "product",
        legacy,
        budget=coordinator,
        source_commit_id="3" * 64,
    )
    store.initialize(initialized_at=NOW)
    request = _request(
        "run-budget",
        transaction_suffix="9",
        publication_kind="product_terminal",
        status="succeeded",
    )
    request = replace(
        request,
        budget_binding=provisional_budget_binding(
            request.run_id,
            request.transaction_id,
            policy,
        ),
    )
    request = store.freeze_budget_binding(request)

    outcome = store.publish(request)
    read = store.read_current(request.run_id)
    budget_state = durable.load(request.run_id)

    assert read.read_status is RunReadStatus.SUCCEEDED
    assert read.current_pointer_sha256 == outcome.pointer_sha256
    assert len(budget_state.settlements) == 1
    assert budget_state.settlements[0].result_sha256 == outcome.pointer_sha256
    assert budget_state.settlements[0].effect_evidence_sha256 == (outcome.completion_marker_sha256)
