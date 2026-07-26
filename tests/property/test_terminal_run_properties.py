from __future__ import annotations

from itertools import permutations

import pytest

from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_canonical import (
    inventory_entry,
    terminal_inventory_sha256,
    verify_payload_inventory,
)


def _entry(name: str, value: bytes):
    return inventory_entry(
        logical_name=name,
        data=value,
        media_type="text/markdown",
        artifact_schema_version="poker-final-report-markdown-artifact-v1",
        serialization="poker-run-storage-utf8-text-v1",
    )


def test_inventory_digest_is_independent_of_caller_order() -> None:
    entries = (
        _entry("final_report.md", b"report"),
        _entry("tool_results/result-1.input.json", b"{}"),
    )

    digests = {terminal_inventory_sha256(candidate) for candidate in permutations(entries)}

    assert len(digests) == 1


def test_one_byte_payload_change_is_detected_before_status_trust() -> None:
    entry = _entry("final_report.md", b"report")

    with pytest.raises(CanonicalStorageError, match="size or hash"):
        verify_payload_inventory((entry,), {"final_report.md": b"reporu"})
