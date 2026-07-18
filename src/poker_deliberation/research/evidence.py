"""Claim-level evidence ledger."""

from __future__ import annotations

from poker_deliberation.schemas import Claim, EvidenceRecord


class EvidenceLedger:
    def __init__(self, records: list[EvidenceRecord] | None = None) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        for record in records or []:
            self.add(record)

    def add(self, record: EvidenceRecord) -> EvidenceRecord:
        if record.evidence_id in self._records:
            raise ValueError(f"duplicate evidence id: {record.evidence_id}")
        if record.url is None and not record.identifier:
            raise ValueError("evidence requires a URL or identifier")
        self._records[record.evidence_id] = record
        return record

    def all(self) -> list[EvidenceRecord]:
        return list(self._records.values())

    def for_claim(self, claim_id: str) -> list[EvidenceRecord]:
        return [
            record for record in self._records.values() if claim_id in record.supported_claim_ids
        ]

    def unsupported_claims(self, claims: list[Claim]) -> list[str]:
        return [claim.claim_id for claim in claims if not self.for_claim(claim.claim_id)]
