from __future__ import annotations

from datetime import UTC, datetime

import pytest

from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from tests.confirmed_review_support import app_config, confirmed_admission


def _published_payloads(tmp_path):
    admission = confirmed_admission(
        run_id="run-confirmed-tamper-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    report = orchestrator.run_confirmed_review(admission)
    read = orchestrator.product_store.read_current(report.run_id)
    return report.run_id, {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "candidate",
        "confirmation",
        "provenance",
        "omit-provenance",
    ],
)
def test_any_confirmed_review_chain_mutation_fails_replay(tmp_path, mutation: str) -> None:
    run_id, payloads = _published_payloads(tmp_path)
    if mutation == "source":
        payloads["confirmed_review_source.txt"] += b"tamper\n"
    elif mutation == "omit-provenance":
        del payloads["confirmed_review_provenance.json"]
    else:
        logical_name = f"confirmed_review_{mutation}.json"
        data = bytearray(payloads[logical_name])
        data[-1] = ord(" ")
        payloads[logical_name] = bytes(data)
    with pytest.raises(CanonicalStorageError):
        product_payload_commitments(
            payloads,
            run_id=run_id,
            status="succeeded",
        )
