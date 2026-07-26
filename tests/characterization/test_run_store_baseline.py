from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.storage.run_store import RunStore


def test_flat_v1_run_store_bytes_layout_and_write_order_remain_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-runs"
    store = RunStore(root)
    run = store.create_run("legacy-1")
    store.write_json("legacy-1", "input.json", {"b": 2, "a": 1})
    store.append_jsonl("legacy-1", "events.jsonl", {"event": 1})
    store.write_text("legacy-1", "final_report.md", "unchanged\n")

    assert (run / ".poker-deliberation-run").read_bytes() == b"v1\n"
    assert (run / "input.json").read_bytes() == b'{\n  "a": 1,\n  "b": 2\n}\n'
    assert (run / "events.jsonl").read_bytes() == b'{"event": 1}\n'
    assert (run / "final_report.md").read_bytes() == b"unchanged\n"
    assert not (run / ".revision-store").exists()


def test_flat_v1_failure_atomicity_limitation_is_only_characterized(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "legacy-runs", max_artifact_bytes=4)
    store.create_run("legacy-1")

    with pytest.raises(ValueError):
        store.write_text("legacy-1", "too-large.txt", "12345")

    assert not (store.run_dir("legacy-1") / "too-large.txt").exists()
