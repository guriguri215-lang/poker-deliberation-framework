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

P2_027B_UNCHANGED_BOUNDARY_SHA256 = {
    "src/poker_deliberation/context_lifecycle.py": (
        "969d03a57af3405b0873766cd53c3f079772b09bc27222691dfa6cafaad5b111"
    ),
    "src/poker_deliberation/schemas.py": (
        "57518549c4312aa7bac97fd13cd455f45798a4a043b0685f31b6393087dfa582"
    ),
    "src/poker_deliberation/tools/registry.py": (
        "cdf1846f72122b6d3252242fb71cd822b16ac957173cb09d4b118ac28e48b32b"
    ),
    "src/poker_deliberation/phases/revision_coordinator.py": (
        "10b457da988a935ce12a68cb7152f1844586160aa2d52b4c2fd23d3dacb6f75b"
    ),
}


def test_p2_027b_unchanged_runtime_boundaries_remain_byte_exact() -> None:
    # P2-013B explicitly adds resume/reissue wiring to orchestrator.py and cli.py.
    # P2-027B's independent context/schema/tool/coordinator boundaries stay frozen.
    observed = {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in P2_027B_UNCHANGED_BOUNDARY_SHA256
    }
    assert observed == P2_027B_UNCHANGED_BOUNDARY_SHA256


def test_cleanup_is_additive_python_api_and_does_not_add_cli_surface() -> None:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == {
        "audit-claim",
        "calculate",
        "doctor",
        "list-agents",
        "list-tools",
        "resume",
        "review-hand",
        "review-strategy",
        "show",
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
