from __future__ import annotations

import pytest

from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    parse_canonical_json,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from tests.bounded_natural_language_support import app_config, bounded_admission


def _published(tmp_path):
    admission = bounded_admission(
        run_id="run-bnl-fault-1",
        intake_id="intake-bnl-fault-1",
    )
    orchestrator = Orchestrator(
        config=app_config(tmp_path / "f"),
        provider=LocalProvider(),
    )
    report = orchestrator.run_bounded_natural_language_review(admission)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {item.inventory.logical_name: item.exact_bytes for item in read.payloads}
    return orchestrator, report, read, payloads


def _replay(orchestrator, report, read, payloads):
    return product_payload_commitments(
        payloads,
        run_id=report.run_id,
        status="succeeded",
        revision=read.revision,
        revision_root=orchestrator.product_store.revision_root,
        transaction_id=read.transaction_id,
        previous_manifest_sha256=None,
        previous_pointer_sha256=None,
    )


@pytest.mark.parametrize(
    "logical_name",
    [
        "bounded_nl_source.txt",
        "bounded_nl_candidate.json",
        "bounded_nl_confirmation.json",
        "bounded_nl_provenance.json",
    ],
)
def test_each_missing_bounded_artifact_breaks_terminal_replay(tmp_path, logical_name: str) -> None:
    orchestrator, report, read, payloads = _published(tmp_path)
    del payloads[logical_name]
    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, payloads)


def test_source_and_report_marker_tamper_break_terminal_replay(tmp_path) -> None:
    orchestrator, report, read, payloads = _published(tmp_path)
    source_tamper = dict(payloads)
    source_tamper["bounded_nl_source.txt"] += b" "
    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, source_tamper)

    report_tamper = dict(payloads)
    report_value = parse_canonical_json(report_tamper["final_report.json"])
    assert isinstance(report_value, dict)
    reconstructed = report_value["reconstructed_input"]
    assert isinstance(reconstructed, dict)
    metadata = reconstructed["metadata"]
    assert isinstance(metadata, dict)
    del metadata["bounded_natural_language_review"]
    report_tamper["final_report.json"] = canonical_json_bytes(report_value)
    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, report_tamper)
