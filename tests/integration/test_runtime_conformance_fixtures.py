"""Execute the tracked version-1 cross-runtime conformance scenarios."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from poker_deliberation.runtime_conformance import (
    ApprovalBindingV1,
    ToolCapabilityAllowlistV1,
    compare_records,
)
from poker_deliberation.runtime_conformance.canonical import (
    APPROVAL_BINDING_DOMAIN,
    canonical_domain_sha256,
)
from tests.runtime_conformance_support import HASH_A, NOW, ROOT, record_pair

FIXTURE = ROOT / "tests" / "fixtures" / "runtime_conformance" / "v1" / "scenarios.json"


def _cases() -> list[dict[str, Any]]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "fixture_version", "cases"}
    assert raw["schema_version"] == "1.0.0"
    assert raw["fixture_version"] == "1.0.0"
    return list(raw["cases"])


def _mutate_target(mutation: str) -> object:
    _, target, _, _ = record_pair()
    if mutation == "none":
        return target
    if mutation == "unknown-role":
        return target.model_copy(
            update={
                "assignment": target.assignment.model_copy(
                    update={"runtime_role_id": "unknown-role"}
                )
            }
        )
    if mutation == "unknown-capability":
        allowlist = ToolCapabilityAllowlistV1(
            policy_version="1.0.0",
            allowed_tools=(),
            allowed_capabilities=("unknown-capability",),
            catalog_status="declared",
            policy_source="fixture",
        )
        return target.model_copy(
            update={"assignment": target.assignment.model_copy(update={"allowlist": allowlist})}
        )
    if mutation == "allowlist-expansion":
        allowlist = ToolCapabilityAllowlistV1(
            policy_version="1.0.0",
            allowed_tools=("pot_odds",),
            allowed_capabilities=(),
            catalog_status="declared",
            policy_source="fixture",
        )
        return target.model_copy(
            update={"assignment": target.assignment.model_copy(update={"allowlist": allowlist})}
        )
    if mutation == "classification-mismatch":
        context = target.assignment.context.model_copy(update={"classification": "public"})
        return target.model_copy(
            update={"assignment": target.assignment.model_copy(update={"context": context})}
        )
    if mutation == "provenance-mismatch":
        provenance = target.assignment.context.provenance.model_copy(
            update={"source_sha256": "f" * 64}
        )
        context = target.assignment.context.model_copy(update={"provenance": provenance})
        return target.model_copy(
            update={"assignment": target.assignment.model_copy(update={"context": context})}
        )
    if mutation == "approval-digest-mismatch":
        approval = ApprovalBindingV1(
            requirement="required",
            request_id="fixture-approval",
            action_digest_sha256=HASH_A,
            decision="approved",
            decision_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
            authority_snapshot_sha256="e" * 64,
        )
        assignment = target.assignment.model_copy(update={"approval": approval})
        audit = target.execution_audit.model_copy(
            update={
                "approval_binding_sha256": canonical_domain_sha256(
                    APPROVAL_BINDING_DOMAIN,
                    approval,
                )
            }
        )
        return target.model_copy(update={"assignment": assignment, "execution_audit": audit})
    if mutation == "audit-hash-mismatch":
        audit = target.execution_audit.model_copy(update={"context_sha256": "f" * 64})
        return target.model_copy(update={"execution_audit": audit})
    raise AssertionError(f"unknown fixture mutation: {mutation}")


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_versioned_conformance_fixture(case: dict[str, Any]) -> None:
    source, _, codex, python = record_pair()
    target = _mutate_target(str(case["mutation"]))
    check = compare_records(
        source,
        target,  # type: ignore[arg-type]
        codex,
        python,
        now=NOW,
    )
    expected = case["expected_code"]

    if expected is None:
        assert check.status == "conformant"
    else:
        assert expected in {item.code for item in check.violations}
