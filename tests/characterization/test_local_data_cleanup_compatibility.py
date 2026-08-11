from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from poker_deliberation.cli import build_parser
from poker_deliberation.context_lifecycle import (
    ATTEMPT_MEMORY_ONLY_RETENTION_POLICY,
    CONTEXT_SCHEMA_VERSION,
)
from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor

ROOT = Path(__file__).resolve().parents[2]

P3_016A_COMPATIBLE_BOUNDARY_SHA256 = {
    "src/poker_deliberation/context_lifecycle.py": (
        "969d03a57af3405b0873766cd53c3f079772b09bc27222691dfa6cafaad5b111"
    ),
    "src/poker_deliberation/schemas.py": (
        "93df8451f9825e0f09b43515df401a723b581cef3c41133118df48a1af4f2256"
    ),
    "src/poker_deliberation/phases/revision_coordinator.py": (
        "10b457da988a935ce12a68cb7152f1844586160aa2d52b4c2fd23d3dacb6f75b"
    ),
}


def test_p2_027b_boundaries_remain_compatible_after_additive_range_schema() -> None:
    # P2-013B explicitly adds resume/reissue wiring to orchestrator.py and cli.py.
    # P3-016A changes only CanonicalHand.known_ranges additively; the legacy
    # RangeDefinition shape and P2-027B context/coordinator behavior stay characterized.
    observed = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in P3_016A_COMPATIBLE_BOUNDARY_SHA256
    }
    assert observed == P3_016A_COMPATIBLE_BOUNDARY_SHA256


def test_cleanup_is_additive_python_api_and_does_not_add_cli_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == {
        "audit-claim",
        "calculate",
        "confirm-bounded-codex-role-request",
        "confirm-bounded-river-call-ev-intake",
        "confirm-bounded-river-review-role-request",
        "confirm-bounded-river-review",
        "confirm-bounded-review-intake",
        "confirm-review-intake",
        "doctor",
        "list-agents",
        "list-tools",
        "prepare-bounded-codex-bridge",
        "prepare-review-intake",
        "prepare-bounded-river-call-ev-intake",
        "prepare-bounded-river-review",
        "prepare-bounded-review-intake",
        "replay-bounded-river-review",
        "resume",
        "resume-bounded-river-review",
        "review-confirmed-intake",
        "review-bounded-river-call-ev-confirmed-intake",
        "review-bounded-confirmed-intake",
        "review-hand",
        "review-strategy",
        "run-bounded-river-review",
        "execute-bounded-codex-role",
        "execute-bounded-river-review-role",
        "replay-bounded-codex-bridge",
        "show",
        "show-bounded-codex-role-request",
        "show-bounded-river-review-role-request",
        "show-bounded-river-review",
        "status-bounded-river-review",
    }
    assert "cleanup" not in subparsers.choices
    assert {
        "dry_run_quarantine",
        "execute",
        "execute_quarantine",
        "dry_run_delete",
        "execute_delete",
        "inspect_reconciliation",
    } <= set(dir(LocalDataCleanupExecutor))


def test_context_lifecycle_boundary_is_unchanged() -> None:
    assert CONTEXT_SCHEMA_VERSION == "1.0.0"
    assert ATTEMPT_MEMORY_ONLY_RETENTION_POLICY == "attempt-memory-only-v1"


def test_tool_registry_change_is_additive_and_keeps_cleanup_out_of_tool_surface() -> None:
    registry_source = (ROOT / "src/poker_deliberation/tools/registry.py").read_text(
        encoding="utf-8"
    )

    assert '"hand_validator"' in registry_source
    assert '"hand_pot_ledger"' in registry_source
    assert '"cleanup"' not in registry_source
