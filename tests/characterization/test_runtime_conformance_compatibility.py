"""Characterize P2-025A as additive to existing product and storage contracts."""

from __future__ import annotations

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.runtime_conformance import CANONICAL_DOMAIN_INVENTORY
from poker_deliberation.schemas import FinalReport
from poker_deliberation.storage.revision_canonical import (
    CONTROL_CANONICALIZATION,
    canonical_json_bytes,
)
from poker_deliberation.storage.terminal_models import (
    TERMINAL_SCHEMA_VERSION,
    TERMINAL_STORAGE_PROTOCOL,
)


def test_existing_artifact_schema_and_storage_domains_are_unchanged() -> None:
    assert tuple(FinalReport.model_fields) == (
        "run_id",
        "run_status",
        "conclusion",
        "reconstructed_input",
        "data_quality",
        "claim_assessments",
        "analysis_sections",
        "agent_execution_records",
        "security_events",
        "tool_results",
        "alternatives",
        "sensitivity",
        "disputes",
        "evidence",
        "reproduction_steps",
        "approvals",
        "confidence",
        "limitations",
        "generated_at",
    )
    assert CONTROL_CANONICALIZATION == "poker-run-storage-json-v1"
    assert TERMINAL_SCHEMA_VERSION == "2.0.0"
    assert TERMINAL_STORAGE_PROTOCOL == "poker-run-terminal-v2"
    assert canonical_json_bytes({"b": 1, "a": "value"}) == b'{"a":"value","b":1}'


def test_conformance_domains_are_dedicated_and_bridge_remains_unavailable() -> None:
    domains = {item.domain for item in CANONICAL_DOMAIN_INVENTORY}
    states = {item.capability_id: item.state for item in CAPABILITIES}

    assert domains == {
        "poker-runtime-conformance-inventory-v1",
        "poker-runtime-conformance-record-v1",
    }
    assert all("poker-run-storage-json-v1" not in domain for domain in domains)
    assert states["codex_python_runtime_bridge"] == "unavailable"
