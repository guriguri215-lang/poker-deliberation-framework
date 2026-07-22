from __future__ import annotations

import pytest

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    SerialUsageLedger,
    UsageDelta,
)


def test_utf8_byte_cap_accepts_exact_value_and_rejects_one_byte_over() -> None:
    exact_payload = "あ" * 341 + "a"
    assert len(exact_payload.encode("utf-8")) == 1024
    ledger = SerialUsageLedger(
        BudgetPolicyV2(max_tool_input_bytes=1024),
        active=False,
    )

    ledger.apply(UsageDelta(tool_input_bytes=len(exact_payload.encode("utf-8"))))
    with pytest.raises(BudgetLimitError) as error:
        ledger.apply(UsageDelta(tool_input_bytes=1))

    assert error.value.failure.code is BudgetFailureCode.TOOL_INPUT_EXCEEDED
    assert ledger.snapshot().tool_input_bytes == 1024


@pytest.mark.parametrize(
    ("delta", "code"),
    [
        (UsageDelta(provider_output_bytes=1025), BudgetFailureCode.PROVIDER_OUTPUT_EXCEEDED),
        (UsageDelta(tool_output_bytes=1025), BudgetFailureCode.TOOL_OUTPUT_EXCEEDED),
        (UsageDelta(artifact_bytes=1025), BudgetFailureCode.ARTIFACT_EXCEEDED),
        (UsageDelta(run_bytes=10_241), BudgetFailureCode.RUN_EXCEEDED),
    ],
)
def test_split_byte_caps_fail_closed_without_committing_rejected_usage(
    delta: UsageDelta,
    code: BudgetFailureCode,
) -> None:
    ledger = SerialUsageLedger(
        BudgetPolicyV2(
            max_provider_output_bytes=1024,
            max_tool_output_bytes=1024,
            max_artifact_bytes=1024,
            max_run_bytes=10_240,
        ),
        active=False,
    )

    with pytest.raises(BudgetLimitError) as error:
        ledger.apply(delta)

    assert error.value.failure.code is code
    snapshot = ledger.snapshot()
    assert snapshot.provider_output_bytes == 0
    assert snapshot.tool_output_bytes == 0
    assert snapshot.artifact_bytes == 0
    assert snapshot.run_bytes == 0


def test_snapshot_from_different_policy_cannot_be_substituted() -> None:
    first = BudgetPolicyV2(max_runtime_seconds=1.0)
    second = BudgetPolicyV2(max_runtime_seconds=2.0)
    snapshot = SerialUsageLedger(first, active=False).snapshot()

    with pytest.raises(ValueError, match="policy hash mismatch"):
        SerialUsageLedger(second, initial=snapshot, active=False)
