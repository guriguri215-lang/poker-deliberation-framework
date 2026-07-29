from __future__ import annotations

from datetime import UTC, datetime

import pytest

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    artifact_table_entry,
    build_inventory,
    classification_evidence_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
)
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
        "omit-all-confirmed-artifacts",
    ],
)
def test_any_confirmed_review_chain_mutation_fails_replay(tmp_path, mutation: str) -> None:
    run_id, payloads = _published_payloads(tmp_path)
    if mutation == "source":
        payloads["confirmed_review_source.txt"] += b"tamper\n"
    elif mutation == "omit-provenance":
        del payloads["confirmed_review_provenance.json"]
    elif mutation == "omit-all-confirmed-artifacts":
        for logical_name in tuple(payloads):
            if logical_name.startswith("confirmed_review_"):
                del payloads[logical_name]
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


def test_structural_revision_rejects_confirmed_artifacts_without_core_chain(tmp_path) -> None:
    run_id, payloads = _published_payloads(tmp_path)
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.INTERNAL,),
        restricted_secret_check_completed=True,
    )
    artifacts: list[RevisionArtifactV1] = []
    for logical_name in (
        "confirmed_review_source.txt",
        "confirmed_review_candidate.json",
        "confirmed_review_confirmation.json",
        "confirmed_review_provenance.json",
    ):
        media_type, serialization, schema, origin = artifact_table_entry(logical_name)
        local = LocalDataBindingV1(
            logical_name=logical_name,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.SOURCE_INHERITANCE,
            classification_evidence=evidence,
            classification_evidence_sha256=classification_evidence_sha256(evidence),
        )
        artifacts.append(
            RevisionArtifactV1(
                logical_name=logical_name,
                media_type=media_type,
                artifact_schema_version=schema,
                serialization=serialization,
                exact_bytes=payloads[logical_name],
                required=True,
                classification=ContextClassification.INTERNAL,
                classification_source=ClassificationSource.SOURCE_INHERITANCE,
                classification_evidence=evidence,
                policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
                origin_kind=origin,
                provenance_bindings=(local,),
            )
        )
    request = RevisionPublishRequestV1(
        run_id=run_id,
        transaction_id="txn-" + "c" * 32,
        proposed_revision=1,
        created_at=datetime.now(UTC),
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=tuple(artifacts),
    )
    with pytest.raises(
        CanonicalStorageError,
        match="marker and complete artifact set",
    ):
        build_inventory(request, max_artifact_bytes=1_000_000)
