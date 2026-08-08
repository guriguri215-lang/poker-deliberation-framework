"""Immutable marker-last storage for bounded Codex bridge artifacts."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pydantic import BaseModel

from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_model,
    sha256_bytes,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BRIDGE_SCHEMA_VERSION,
    CONFIRMATION_IDENTIFIER_CLAIM_HASH_DOMAIN,
    EXECUTION_IDENTITY_CLAIM_HASH_DOMAIN,
    TERMINAL_MANIFEST_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeArtifactInventoryV1,
    BridgeArtifactKind,
    BridgeCompletionMarkerV1,
    BridgeConfirmationIdentifierClaimV1,
    BridgeCurrentPointerV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgeExecutionIdentityClaimV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    BridgeTerminalManifestV1,
    BridgeTerminalStatus,
    RuntimeAuthModeV1,
)
from poker_deliberation.storage.directory_durability import sync_directory
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.revision_lock import (
    AuthorityLease,
    acquire_authority,
    verify_directory,
    verify_regular_single_link,
)

MAX_BRIDGE_ARTIFACT_BYTES = 1_000_000
MAX_BRIDGE_REVISION_BYTES = 8_000_000
_LOGICAL_NAME = re.compile(r"^[a-z0-9][a-z0-9_./-]{0,191}$")
_RUN_DIRECTORY = re.compile(r"^[0-9a-f]{32}$")
_REVISION_DIRECTORY = re.compile(r"^r(?P<revision>[1-9][0-9]*)-(?P<txn>txn-[0-9a-f]{32})$")
_IDENTITY_CLAIM = re.compile(r"^(?P<kind>thread|turn)-(?P<sha>[0-9a-f]{64})\.json$")
_CONFIRMATION_IDENTIFIER_CLAIM = re.compile(
    r"^(?P<kind>confirmation|idempotency)-(?P<sha>[0-9a-f]{64})\.json$"
)

BridgeArtifactModel: TypeAlias = (
    BridgeRunPlanV1
    | BridgeSourceContextV1
    | BoundedCodexBridgeRequestV1
    | BridgeRoleConfirmationV1
    | BridgePreExecutionAdmissionV1
    | BridgeRoleResultV1
    | BridgeExecutionAuditV1
)
BridgeCompletionStatus: TypeAlias = Literal[
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "cancel_unconfirmed",
    "effect_unknown",
]

_ARTIFACT_MODELS: dict[BridgeArtifactKind, type[BaseModel]] = {
    "run_plan": BridgeRunPlanV1,
    "source_context": BridgeSourceContextV1,
    "request": BoundedCodexBridgeRequestV1,
    "confirmation": BridgeRoleConfirmationV1,
    "admission": BridgePreExecutionAdmissionV1,
    "role_result": BridgeRoleResultV1,
    "execution_audit": BridgeExecutionAuditV1,
}


class BridgeStorageError(CanonicalStorageError):
    """Raised when bridge publication or replay cannot be proven exact."""


class BridgeExecutionIdentityCollisionError(BridgeStorageError):
    """Raised only when a thread or turn identity already has a durable claim."""


@dataclass(frozen=True, slots=True)
class BridgeStoredArtifact:
    logical_name: str
    artifact_kind: BridgeArtifactKind
    model: BridgeArtifactModel


@dataclass(frozen=True, slots=True)
class BridgePublishRequest:
    run_plan: BridgeRunPlanV1
    status: BridgeTerminalStatus
    proposed_revision: int
    transaction_id: str
    expected_revision: int | None
    expected_manifest_sha256: str | None
    expected_pointer_sha256: str | None
    published_at: datetime
    artifacts: tuple[BridgeStoredArtifact, ...]


@dataclass(frozen=True, slots=True)
class BridgePublishOutcome:
    bridge_run_id: str
    revision: int
    transaction_id: str
    manifest_sha256: str
    pointer_sha256: str
    completion_marker_sha256: str | None


@dataclass(frozen=True, slots=True)
class VerifiedBridgeRead:
    pointer: BridgeCurrentPointerV1
    pointer_sha256: str
    manifest: BridgeTerminalManifestV1
    manifest_bytes: bytes
    completion_marker: BridgeCompletionMarkerV1 | None
    completion_marker_bytes: bytes | None
    artifacts: Mapping[str, bytes]

    def artifact_bytes(self, logical_name: str) -> bytes:
        try:
            return self.artifacts[logical_name]
        except KeyError as exc:
            raise BridgeStorageError("bridge artifact is absent") from exc

    def decoded_artifacts(self) -> tuple[BridgeStoredArtifact, ...]:
        decoded: list[BridgeStoredArtifact] = []
        for entry in self.manifest.inventory:
            model = parse_canonical_model(
                self.artifact_bytes(entry.logical_name),
                _ARTIFACT_MODELS[entry.artifact_kind],
            )
            decoded.append(
                BridgeStoredArtifact(
                    logical_name=entry.logical_name,
                    artifact_kind=entry.artifact_kind,
                    model=cast(BridgeArtifactModel, model),
                )
            )
        return tuple(decoded)


@dataclass(frozen=True, slots=True)
class _PreparedPublication:
    request: BridgePublishRequest
    artifact_bytes: Mapping[str, bytes]
    manifest: BridgeTerminalManifestV1
    manifest_bytes: bytes
    completion_marker: BridgeCompletionMarkerV1 | None
    completion_marker_bytes: bytes | None
    pointer: BridgeCurrentPointerV1
    pointer_bytes: bytes


def _validate_logical_name(value: str) -> str:
    if _LOGICAL_NAME.fullmatch(value) is None or "\\" in value:
        raise BridgeStorageError("invalid bridge artifact logical name")
    parts = Path(value).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise BridgeStorageError("bridge artifact path is not confined")
    return value


def _read_bounded(path: Path, *, maximum: int = MAX_BRIDGE_ARTIFACT_BYTES) -> bytes:
    info = verify_regular_single_link(path)
    if info.st_size < 1 or info.st_size > maximum:
        raise BridgeStorageError("bridge artifact size is outside the bound")
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise BridgeStorageError("bridge artifact changed while it was read")
    return data


def _write_exclusive(path: Path, data: bytes) -> None:
    if not data or len(data) > MAX_BRIDGE_ARTIFACT_BYTES:
        raise BridgeStorageError("bridge artifact size is outside the bound")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if _read_bounded(path) != data:
        raise BridgeStorageError("bridge artifact write verification failed")


def _artifact_inventory(
    artifacts: tuple[BridgeStoredArtifact, ...],
) -> tuple[tuple[BridgeArtifactInventoryV1, ...], dict[str, bytes]]:
    encoded: dict[str, bytes] = {}
    kinds: dict[str, BridgeArtifactKind] = {}
    for artifact in artifacts:
        name = _validate_logical_name(artifact.logical_name)
        if name in encoded:
            raise BridgeStorageError("duplicate bridge artifact logical name")
        expected_model = _ARTIFACT_MODELS[artifact.artifact_kind]
        if not isinstance(artifact.model, expected_model):
            raise BridgeStorageError("bridge artifact kind and schema disagree")
        data = canonical_json_bytes(artifact.model)
        if len(data) > MAX_BRIDGE_ARTIFACT_BYTES:
            raise BridgeStorageError("bridge artifact exceeds the byte bound")
        encoded[name] = data
        kinds[name] = artifact.artifact_kind
    if set(encoded) < {"run_plan.json", "source_context.json"}:
        raise BridgeStorageError("bridge revision lacks its immutable run anchors")
    ordered_names = tuple(sorted(encoded, key=lambda item: item.encode("utf-8")))
    inventory = tuple(
        BridgeArtifactInventoryV1(
            logical_name=name,
            artifact_kind=kinds[name],
            sha256=sha256_bytes(encoded[name]),
            size_bytes=len(encoded[name]),
        )
        for name in ordered_names
    )
    if sum(item.size_bytes for item in inventory) > MAX_BRIDGE_REVISION_BYTES:
        raise BridgeStorageError("bridge revision exceeds the byte bound")
    return inventory, encoded


def _role_artifact_name(role: BridgeRole, artifact: str) -> str:
    return f"roles/{BRIDGE_ROLE_ORDER.index(role)}/{artifact}.json"


def _verify_execution_progression(
    artifacts: Mapping[str, bytes],
    *,
    status: BridgeTerminalStatus,
    completion_marker_present: bool,
) -> None:
    """Reject non-serial or internally incomplete execution artifact states."""

    completed: list[BridgeRole] = []
    open_admissions = 0
    admitted_roles: list[BridgeRole] = []
    request_present: dict[BridgeRole, bool] = {}
    for ordinal, role in enumerate(BRIDGE_ROLE_ORDER):
        names = {
            artifact: _role_artifact_name(role, artifact)
            for artifact in ("request", "confirmation", "admission", "result", "audit")
        }
        present = {artifact for artifact, name in names.items() if name in artifacts}
        request_present[role] = "request" in present
        if present - {"request"} and "request" not in present:
            raise BridgeStorageError("bridge execution artifact lacks its role request")
        if "admission" in present and "confirmation" not in present:
            raise BridgeStorageError("bridge admission lacks its confirmation")
        if "result" in present and not {"admission", "audit"}.issubset(present):
            raise BridgeStorageError("bridge role result lacks admission or audit")
        if "audit" in present and "admission" not in present:
            raise BridgeStorageError("bridge execution audit lacks its admission")
        if "admission" in present:
            admitted_roles.append(role)

        for artifact in present:
            model = parse_canonical_model(
                artifacts[names[artifact]],
                _ARTIFACT_MODELS[
                    cast(
                        BridgeArtifactKind,
                        {
                            "request": "request",
                            "confirmation": "confirmation",
                            "admission": "admission",
                            "result": "role_result",
                            "audit": "execution_audit",
                        }[artifact],
                    )
                ],
            )
            model_role = (
                model.context.assignment.role
                if isinstance(model, BoundedCodexBridgeRequestV1)
                else (
                    model.output.role
                    if isinstance(model, BridgeRoleResultV1)
                    else getattr(model, "role", None)
                )
            )
            if model_role is not role:
                raise BridgeStorageError("bridge execution artifact role binding mismatch")

        if "admission" in present and "audit" not in present:
            open_admissions += 1
        if "audit" not in present:
            continue
        audit = parse_canonical_model(
            artifacts[names["audit"]],
            BridgeExecutionAuditV1,
        )
        succeeded = audit.effect_state is BridgeEffectState.SUCCEEDED
        if succeeded != ("result" in present):
            raise BridgeStorageError("bridge execution success/result matrix is invalid")
        if succeeded:
            if tuple(completed) != BRIDGE_ROLE_ORDER[:ordinal]:
                raise BridgeStorageError("bridge completed roles are not a continuous prefix")
            completed.append(role)

    if open_admissions > 1:
        raise BridgeStorageError("bridge has more than one open execution admission")
    for role in admitted_roles:
        ordinal = BRIDGE_ROLE_ORDER.index(role)
        if tuple(completed[:ordinal]) != BRIDGE_ROLE_ORDER[:ordinal]:
            raise BridgeStorageError("bridge execution admission is out of serial order")
    if open_admissions and status not in {"approval_required", "in_progress"}:
        raise BridgeStorageError("terminal bridge revision has an open execution admission")
    first_three_complete = tuple(completed[:3]) == BRIDGE_ROLE_ORDER[:3]
    if request_present[BridgeRole.ADJUDICATOR] != first_three_complete:
        raise BridgeStorageError("bridge adjudicator request dependency is invalid")
    adjudicator_complete = BridgeRole.ADJUDICATOR in completed
    if request_present[BridgeRole.REPORT_WRITER] != adjudicator_complete:
        raise BridgeStorageError("bridge report-writer request dependency is invalid")
    all_roles_complete = tuple(completed) == BRIDGE_ROLE_ORDER
    if (status == "succeeded") != all_roles_complete:
        raise BridgeStorageError("bridge succeeded status and completed roles disagree")
    if status == "succeeded" and not completion_marker_present:
        raise BridgeStorageError("successful bridge revision lacks its completion marker")


class BoundedCodexBridgeStore:
    """CAS-published, immutable revisions for one bounded bridge namespace."""

    def __init__(
        self,
        root: Path,
        *,
        transaction_id_factory: Callable[[], str] | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self.transaction_id_factory = transaction_id_factory or (lambda: f"txn-{uuid.uuid4().hex}")
        self.fault_injector = fault_injector

    def _fault(self, hook: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(hook)

    @staticmethod
    def new_transaction_id() -> str:
        return f"txn-{uuid.uuid4().hex}"

    def _run_key(self, bridge_run_id: str) -> str:
        return domain_sha256("poker-bounded-codex-bridge-run-id-v1", bridge_run_id)[:32]

    def _paths(self, bridge_run_id: str) -> tuple[Path, Path, Path, Path, Path]:
        run = self.root / self._run_key(bridge_run_id)
        control = run / ".b"
        return (
            run,
            control,
            control / "t",
            control / "r",
            control / "c.json",
        )

    def _bootstrap(self, bridge_run_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.root)
        run, control, transactions, revisions, _current = self._paths(bridge_run_id)
        for path in (run, control, transactions, revisions):
            path.mkdir(exist_ok=True)
            verify_directory(path)
        sync_directory(revisions, hook="codex_bridge.bootstrap.revisions")
        sync_directory(transactions, hook="codex_bridge.bootstrap.transactions")
        sync_directory(control, hook="codex_bridge.bootstrap.control")
        sync_directory(run, hook="codex_bridge.bootstrap.run")
        sync_directory(self.root, hook="codex_bridge.bootstrap.root")

    def _bootstrap_identities(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.root)
        identities = self.root / ".i"
        identities.mkdir(exist_ok=True)
        verify_directory(identities)
        sync_directory(identities, hook="codex_bridge.identities.bootstrap")
        sync_directory(self.root, hook="codex_bridge.identities.root")

    def _bootstrap_confirmation_identifiers(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.root)
        confirmations = self.root / ".c"
        confirmations.mkdir(exist_ok=True)
        verify_directory(confirmations)
        sync_directory(confirmations, hook="codex_bridge.confirmations.bootstrap")
        sync_directory(self.root, hook="codex_bridge.confirmations.root")

    def _identity_authority(self) -> AuthorityLease:
        identities = self.root / ".i"
        return acquire_authority(
            identities / "authority.lock",
            registry_keys=(f"codex-bridge-identities:{self.root.resolve()}",),
            bootstrap=True,
            prepare=self._bootstrap_identities,
        )

    def _confirmation_identifier_authority(self) -> AuthorityLease:
        confirmations = self.root / ".c"
        return acquire_authority(
            confirmations / "authority.lock",
            registry_keys=(f"codex-bridge-confirmations:{self.root.resolve()}",),
            bootstrap=True,
            prepare=self._bootstrap_confirmation_identifiers,
        )

    def _verify_identity_namespace(self) -> None:
        identities = self.root / ".i"
        verify_directory(identities)
        for entry in identities.iterdir():
            if entry.name == "authority.lock":
                verify_regular_single_link(entry)
                continue
            match = _IDENTITY_CLAIM.fullmatch(entry.name)
            if match is None:
                raise BridgeStorageError("unknown execution identity registry entry")
            claim = parse_canonical_model(
                _read_bounded(entry),
                BridgeExecutionIdentityClaimV1,
            )
            if claim.identity_kind != match.group("kind") or claim.identity_sha256 != match.group(
                "sha"
            ):
                raise BridgeStorageError("execution identity claim filename mismatch")

    def _verify_confirmation_identifier_namespace(self) -> None:
        confirmations = self.root / ".c"
        verify_directory(confirmations)
        for entry in confirmations.iterdir():
            if entry.name == "authority.lock":
                verify_regular_single_link(entry)
                continue
            match = _CONFIRMATION_IDENTIFIER_CLAIM.fullmatch(entry.name)
            if match is None:
                raise BridgeStorageError("unknown confirmation identifier registry entry")
            claim = parse_canonical_model(
                _read_bounded(entry),
                BridgeConfirmationIdentifierClaimV1,
            )
            if claim.identifier_kind != match.group(
                "kind"
            ) or claim.identifier_sha256 != match.group("sha"):
                raise BridgeStorageError("confirmation identifier claim filename mismatch")

    def _authority(self, bridge_run_id: str) -> AuthorityLease:
        run, control, _transactions, _revisions, _current = self._paths(bridge_run_id)
        return acquire_authority(
            control / "authority.lock",
            registry_keys=(f"codex-bridge:{self.root.resolve()}:{run.name}",),
            bootstrap=True,
            prepare=lambda: self._bootstrap(bridge_run_id),
        )

    def _verify_namespace(self, bridge_run_id: str) -> None:
        verify_directory(self.root)
        for sibling in self.root.iterdir():
            if sibling.name == ".i":
                self._verify_identity_namespace()
                continue
            if sibling.name == ".c":
                self._verify_confirmation_identifier_namespace()
                continue
            if _RUN_DIRECTORY.fullmatch(sibling.name) is None:
                raise BridgeStorageError("unknown bridge store root entry")
            verify_directory(sibling)
        run, control, transactions, revisions, current = self._paths(bridge_run_id)
        verify_directory(run)
        if set(item.name for item in run.iterdir()) != {".b"}:
            raise BridgeStorageError("unknown bridge run namespace entry")
        verify_directory(control)
        allowed = {"authority.lock", "t", "r", "c.json"}
        for entry in control.iterdir():
            if entry.name.startswith("c.txn-") and entry.name.endswith(".tmp"):
                verify_regular_single_link(entry)
            elif entry.name not in allowed:
                raise BridgeStorageError("unknown bridge control entry")
        verify_regular_single_link(control / "authority.lock")
        verify_directory(transactions)
        verify_directory(revisions)
        if current.exists():
            verify_regular_single_link(current)
        for entry in transactions.iterdir():
            verify_directory(entry)
        for entry in revisions.iterdir():
            if _REVISION_DIRECTORY.fullmatch(entry.name) is None:
                raise BridgeStorageError("unknown bridge revision entry")
            verify_directory(entry)

    def claim_confirmation_identifiers(
        self,
        *,
        bridge_run_id: str,
        auth_mode: RuntimeAuthModeV1,
        role: BridgeRole,
        request_sha256: str,
        confirmation_id: str,
        idempotency_key: str,
    ) -> None:
        """Reserve confirmation identifiers across all runs in this store.

        Re-acquiring an identical binding is allowed so a crash after the registry
        write but before the run CAS can be repaired without minting new identifiers.
        """

        confirmations = self.root / ".c"
        claims: list[tuple[Path, BridgeConfirmationIdentifierClaimV1]] = []
        for kind, identifier in (
            ("confirmation", confirmation_id),
            ("idempotency", idempotency_key),
        ):
            identifier_sha = domain_sha256(
                "poker-bounded-codex-bridge-confirmation-identifier-v1",
                identifier,
            )
            payload: dict[str, object] = {
                "schema_version": BRIDGE_SCHEMA_VERSION,
                "identifier_kind": kind,
                "identifier_sha256": identifier_sha,
                "bridge_run_id": bridge_run_id,
                "auth_mode": auth_mode,
                "role": role,
                "request_sha256": request_sha256,
            }
            claim = BridgeConfirmationIdentifierClaimV1.model_validate(
                {
                    **payload,
                    "claim_sha256": domain_sha256(
                        CONFIRMATION_IDENTIFIER_CLAIM_HASH_DOMAIN,
                        payload,
                    ),
                },
                strict=True,
            )
            claims.append((confirmations / f"{kind}-{identifier_sha}.json", claim))
        with self._confirmation_identifier_authority():
            self._verify_confirmation_identifier_namespace()
            # A deleted historical claim must not become available for a different
            # run. Verify every published current revision before reserving any key.
            self._verify_published_current_runs()
            for path, claim in claims:
                if not path.exists():
                    continue
                existing = parse_canonical_model(
                    _read_bounded(path),
                    BridgeConfirmationIdentifierClaimV1,
                )
                if existing != claim:
                    raise BridgeStorageError("bridge confirmation identifier was reused")
            for path, claim in claims:
                if path.exists():
                    continue
                _write_exclusive(path, canonical_json_bytes(claim))
                sync_directory(confirmations, hook="codex_bridge.confirmations.claim")
            self._verify_confirmation_identifier_namespace()

    def _verify_published_current_runs(self) -> None:
        for sibling in sorted(self.root.iterdir(), key=lambda item: item.name.encode("utf-8")):
            if _RUN_DIRECTORY.fullmatch(sibling.name) is None:
                continue
            current = sibling / ".b" / "c.json"
            if not current.exists():
                continue
            pointer = parse_canonical_model(
                _read_bounded(current),
                BridgeCurrentPointerV1,
            )
            if self._run_key(pointer.bridge_run_id) != sibling.name:
                raise BridgeStorageError("bridge current pointer is cross-run")
            verified = self.read_current(pointer.bridge_run_id)
            if any(
                isinstance(item.model, BridgeExecutionAuditV1)
                and item.model.failure_reason_code == "execution_identity_registry_corrupt"
                for item in verified.decoded_artifacts()
            ):
                raise BridgeStorageError("execution identity registry requires reconciliation")

    def verify_execution_identity_history(self) -> None:
        """Fail closed if any published execution has lost its identity claims."""

        try:
            with self._identity_authority():
                self._verify_identity_namespace()
                self._verify_published_current_runs()
                self._verify_identity_namespace()
        except BridgeStorageError:
            raise
        except Exception as exc:
            raise BridgeStorageError(
                "execution identity registry history verification failed"
            ) from exc

    def claim_execution_identity(self, audit: BridgeExecutionAuditV1) -> None:
        """Exclusively reserve runtime thread/turn hashes across this bridge namespace."""

        if audit.thread_id_sha256 is None and audit.turn_id_sha256 is None:
            return
        if audit.thread_id_sha256 is None:
            raise BridgeStorageError("execution turn identity lacks its thread identity")
        claims: list[tuple[Path, BridgeExecutionIdentityClaimV1]] = []
        identities = self.root / ".i"
        observed_identities: tuple[tuple[Literal["thread", "turn"], str], ...] = (
            ("thread", audit.thread_id_sha256),
            *((("turn", audit.turn_id_sha256),) if audit.turn_id_sha256 is not None else ()),
        )
        for kind, identity_sha in observed_identities:
            payload: dict[str, object] = {
                "schema_version": BRIDGE_SCHEMA_VERSION,
                "identity_kind": kind,
                "identity_sha256": identity_sha,
                "bridge_run_id": audit.bridge_run_id,
                "auth_mode": audit.auth_mode,
                "role": audit.role,
                "assignment_id": audit.assignment_id,
                "attempt_id": audit.attempt_id,
                "request_sha256": audit.request_sha256,
                "execution_audit_sha256": audit.audit_sha256,
            }
            claim = BridgeExecutionIdentityClaimV1.model_validate(
                {
                    **payload,
                    "claim_sha256": domain_sha256(
                        EXECUTION_IDENTITY_CLAIM_HASH_DOMAIN,
                        payload,
                    ),
                },
                strict=True,
            )
            claims.append((identities / f"{kind}-{identity_sha}.json", claim))
        try:
            with self._identity_authority():
                self._verify_identity_namespace()
                # Repeat the store-wide check under the same authority as reservation.
                # This closes the gap after the controller's pre-launch check.
                self._verify_published_current_runs()
                if any(path.exists() for path, _claim in claims):
                    raise BridgeExecutionIdentityCollisionError(
                        "execution thread or turn identity was reused"
                    )
                for path, claim in claims:
                    _write_exclusive(path, canonical_json_bytes(claim))
                    sync_directory(identities, hook="codex_bridge.identities.claim")
                self._verify_identity_namespace()
        except BridgeExecutionIdentityCollisionError:
            raise
        except BridgeStorageError:
            raise
        except Exception as exc:
            raise BridgeStorageError("execution identity registry reservation failed") from exc

    def _verify_current_identity_claims(
        self,
        manifest: BridgeTerminalManifestV1,
        artifacts: Mapping[str, bytes],
    ) -> None:
        identities = self.root / ".i"
        for entry in manifest.inventory:
            if entry.artifact_kind != "execution_audit":
                continue
            audit = parse_canonical_model(
                artifacts[entry.logical_name],
                BridgeExecutionAuditV1,
            )
            if audit.thread_id_sha256 is None and audit.turn_id_sha256 is None:
                continue
            if audit.thread_id_sha256 is None:
                raise BridgeStorageError("execution turn identity lacks its thread identity")
            collision_rejected = (
                audit.effect_state is BridgeEffectState.EFFECT_UNKNOWN
                and audit.failure_reason_code == "execution_identity_registry_rejected"
            )
            registry_corrupt = (
                audit.effect_state is BridgeEffectState.EFFECT_UNKNOWN
                and audit.failure_reason_code == "execution_identity_registry_corrupt"
            )
            collision_proved = False
            observed_identities: tuple[tuple[Literal["thread", "turn"], str], ...] = (
                ("thread", audit.thread_id_sha256),
                *((("turn", audit.turn_id_sha256),) if audit.turn_id_sha256 is not None else ()),
            )
            for kind, identity_sha in observed_identities:
                path = identities / f"{kind}-{identity_sha}.json"
                if (collision_rejected or registry_corrupt) and not path.exists():
                    continue
                try:
                    claim = parse_canonical_model(
                        _read_bounded(path),
                        BridgeExecutionIdentityClaimV1,
                    )
                except Exception as exc:
                    raise BridgeStorageError(
                        "execution audit identity claim is missing or invalid"
                    ) from exc
                if claim.identity_kind != kind or claim.identity_sha256 != identity_sha:
                    raise BridgeStorageError("execution audit identity claim mismatch")
                if registry_corrupt:
                    # A post-launch registry fault can leave either no reservation or
                    # a partially written reservation. The terminal audit preserves
                    # the observed hashes, while store-wide admission remains blocked
                    # by _verify_published_current_runs until reconciliation.
                    continue
                bound_to_audit = (
                    claim.bridge_run_id == audit.bridge_run_id
                    and claim.auth_mode is audit.auth_mode
                    and claim.role is audit.role
                    and claim.assignment_id == audit.assignment_id
                    and claim.attempt_id == audit.attempt_id
                    and claim.request_sha256 == audit.request_sha256
                    and claim.execution_audit_sha256 == audit.audit_sha256
                )
                if collision_rejected:
                    collision_proved = collision_proved or not bound_to_audit
                elif not bound_to_audit:
                    raise BridgeStorageError("execution audit identity claim mismatch")
            if collision_rejected and not collision_proved:
                raise BridgeStorageError(
                    "execution identity registry rejection lacks collision evidence"
                )

    def _verify_current_confirmation_identifier_claims(
        self,
        manifest: BridgeTerminalManifestV1,
        artifacts: Mapping[str, bytes],
    ) -> None:
        confirmations = self.root / ".c"
        for entry in manifest.inventory:
            if entry.artifact_kind != "confirmation":
                continue
            confirmation = parse_canonical_model(
                artifacts[entry.logical_name],
                BridgeRoleConfirmationV1,
            )
            for kind, identifier in (
                ("confirmation", confirmation.confirmation_id),
                ("idempotency", confirmation.idempotency_key),
            ):
                identifier_sha = domain_sha256(
                    "poker-bounded-codex-bridge-confirmation-identifier-v1",
                    identifier,
                )
                path = confirmations / f"{kind}-{identifier_sha}.json"
                try:
                    claim = parse_canonical_model(
                        _read_bounded(path),
                        BridgeConfirmationIdentifierClaimV1,
                    )
                except Exception as exc:
                    raise BridgeStorageError(
                        "bridge confirmation identifier claim is missing or invalid"
                    ) from exc
                if (
                    claim.identifier_kind != kind
                    or claim.identifier_sha256 != identifier_sha
                    or claim.bridge_run_id != manifest.bridge_run_id
                    or claim.auth_mode is not manifest.auth_mode
                    or claim.role is not confirmation.role
                    or claim.request_sha256 != confirmation.request_sha256
                ):
                    raise BridgeStorageError(
                        "bridge confirmation identifier claim binding mismatch"
                    )

    def prepare_request(
        self,
        *,
        run_plan: BridgeRunPlanV1,
        status: BridgeTerminalStatus,
        expected: VerifiedBridgeRead | None,
        published_at: datetime,
        artifacts: tuple[BridgeStoredArtifact, ...],
    ) -> BridgePublishRequest:
        revision = 1 if expected is None else expected.pointer.revision + 1
        return BridgePublishRequest(
            run_plan=run_plan,
            status=status,
            proposed_revision=revision,
            transaction_id=self.transaction_id_factory(),
            expected_revision=None if expected is None else expected.pointer.revision,
            expected_manifest_sha256=(
                None if expected is None else expected.manifest.manifest_sha256
            ),
            expected_pointer_sha256=(None if expected is None else expected.pointer_sha256),
            published_at=published_at,
            artifacts=artifacts,
        )

    def _prepare(self, request: BridgePublishRequest) -> _PreparedPublication:
        plan = request.run_plan
        if request.proposed_revision < 1:
            raise BridgeStorageError("invalid bridge revision")
        if re.fullmatch(r"txn-[0-9a-f]{32}", request.transaction_id) is None:
            raise BridgeStorageError("invalid bridge transaction ID")
        if request.proposed_revision == 1:
            if any(
                value is not None
                for value in (
                    request.expected_revision,
                    request.expected_manifest_sha256,
                    request.expected_pointer_sha256,
                )
            ):
                raise BridgeStorageError("initial bridge CAS fields are not empty")
        elif (
            request.expected_revision != request.proposed_revision - 1
            or request.expected_manifest_sha256 is None
            or request.expected_pointer_sha256 is None
        ):
            raise BridgeStorageError("successor bridge CAS fields are incomplete")
        inventory, artifact_bytes = _artifact_inventory(request.artifacts)
        parsed_plan = parse_canonical_model(artifact_bytes["run_plan.json"], BridgeRunPlanV1)
        parsed_source = parse_canonical_model(
            artifact_bytes["source_context.json"], BridgeSourceContextV1
        )
        if parsed_plan != plan or parsed_source.source != plan.source:
            raise BridgeStorageError("bridge immutable anchors are not run-plan bound")
        for entry in inventory:
            model = parse_canonical_model(
                artifact_bytes[entry.logical_name],
                _ARTIFACT_MODELS[entry.artifact_kind],
            )
            artifact_mode = getattr(model, "auth_mode", plan.auth_mode)
            if artifact_mode is not plan.auth_mode:
                raise BridgeStorageError("bridge artifact crosses runtime/auth modes")
        inventory_sha = domain_sha256(
            "poker-bounded-codex-bridge-inventory-v1",
            [item.model_dump(mode="json") for item in inventory],
        )
        manifest_payload: dict[str, object] = {
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "storage_protocol": "poker-bounded-codex-bridge-terminal-v1",
            "bridge_run_id": plan.bridge_run_id,
            "auth_mode": plan.auth_mode,
            "runtime_policy_sha256": plan.runtime_policy_sha256,
            "revision": request.proposed_revision,
            "transaction_id": request.transaction_id,
            "previous_manifest_sha256": request.expected_manifest_sha256,
            "expected_pointer_sha256": request.expected_pointer_sha256,
            "status": request.status,
            "source_terminal_manifest_sha256": (plan.source.source_terminal_manifest_sha256),
            "run_plan_sha256": plan.plan_sha256,
            "created_at": plan.created_at,
            "published_at": request.published_at,
            "inventory": inventory,
            "inventory_sha256": inventory_sha,
        }
        manifest = BridgeTerminalManifestV1.model_validate(
            {
                **manifest_payload,
                "manifest_sha256": domain_sha256(TERMINAL_MANIFEST_HASH_DOMAIN, manifest_payload),
            },
            strict=True,
        )
        manifest_bytes = canonical_json_bytes(manifest)
        terminal = request.status not in {"approval_required", "in_progress"}
        marker: BridgeCompletionMarkerV1 | None = None
        marker_bytes: bytes | None = None
        marker_sha: str | None = None
        if terminal:
            marker = BridgeCompletionMarkerV1(
                bridge_run_id=plan.bridge_run_id,
                auth_mode=plan.auth_mode,
                terminal_revision=request.proposed_revision,
                terminal_transaction_id=request.transaction_id,
                terminal_status=cast(
                    BridgeCompletionStatus,
                    request.status,
                ),
                terminal_manifest_sha256=manifest.manifest_sha256,
                inventory_sha256=inventory_sha,
                published_at=request.published_at,
            )
            marker_bytes = canonical_json_bytes(marker)
            marker_sha = sha256_bytes(marker_bytes)
        pointer = BridgeCurrentPointerV1(
            bridge_run_id=plan.bridge_run_id,
            auth_mode=plan.auth_mode,
            revision=request.proposed_revision,
            transaction_id=request.transaction_id,
            status=request.status,
            manifest_sha256=manifest.manifest_sha256,
            inventory_sha256=inventory_sha,
            completion_marker_sha256=marker_sha,
            published_at=request.published_at,
        )
        return _PreparedPublication(
            request=request,
            artifact_bytes=artifact_bytes,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            completion_marker=marker,
            completion_marker_bytes=marker_bytes,
            pointer=pointer,
            pointer_bytes=canonical_json_bytes(pointer),
        )

    @staticmethod
    def _outcome(pointer: BridgeCurrentPointerV1) -> BridgePublishOutcome:
        return BridgePublishOutcome(
            bridge_run_id=pointer.bridge_run_id,
            revision=pointer.revision,
            transaction_id=pointer.transaction_id,
            manifest_sha256=pointer.manifest_sha256,
            pointer_sha256=sha256_bytes(canonical_json_bytes(pointer)),
            completion_marker_sha256=pointer.completion_marker_sha256,
        )

    @staticmethod
    def _revision_candidates(revisions: Path, ordinal: int) -> tuple[Path, ...]:
        return tuple(
            sorted(
                (
                    path
                    for path in revisions.iterdir()
                    if (match := _REVISION_DIRECTORY.fullmatch(path.name)) is not None
                    and int(match.group("revision")) == ordinal
                ),
                key=lambda path: path.name.encode("utf-8"),
            )
        )

    def _pointer_for_revision(
        self,
        bridge_run_id: str,
        revision: Path,
    ) -> tuple[
        BridgeCurrentPointerV1,
        BridgeTerminalManifestV1,
        BridgeCompletionMarkerV1 | None,
        dict[str, bytes],
    ]:
        match = _REVISION_DIRECTORY.fullmatch(revision.name)
        if match is None:
            raise BridgeStorageError("invalid bridge orphan revision path")
        manifest = parse_canonical_model(
            _read_bounded(revision / "manifest.json"),
            BridgeTerminalManifestV1,
        )
        if (
            manifest.bridge_run_id != bridge_run_id
            or manifest.revision != int(match.group("revision"))
            or manifest.transaction_id != match.group("txn")
        ):
            raise BridgeStorageError("bridge orphan revision path binding mismatch")
        completion = revision / "completion.json"
        pointer = BridgeCurrentPointerV1(
            bridge_run_id=manifest.bridge_run_id,
            auth_mode=manifest.auth_mode,
            revision=manifest.revision,
            transaction_id=manifest.transaction_id,
            status=manifest.status,
            manifest_sha256=manifest.manifest_sha256,
            inventory_sha256=manifest.inventory_sha256,
            completion_marker_sha256=(
                sha256_bytes(_read_bounded(completion)) if completion.exists() else None
            ),
            published_at=manifest.published_at,
        )
        verified_manifest, _manifest_bytes, marker, _marker_bytes, artifacts = self._read_revision(
            bridge_run_id, pointer
        )
        return pointer, verified_manifest, marker, artifacts

    @staticmethod
    def _verify_direct_child(
        pointer: BridgeCurrentPointerV1,
        manifest: BridgeTerminalManifestV1,
        expected: VerifiedBridgeRead | None,
    ) -> None:
        if expected is None:
            if (
                pointer.revision != 1
                or manifest.previous_manifest_sha256 is not None
                or manifest.expected_pointer_sha256 is not None
            ):
                raise BridgeStorageError("initial bridge orphan lineage mismatch")
            return
        if (
            pointer.auth_mode is not expected.pointer.auth_mode
            or pointer.revision != expected.pointer.revision + 1
            or manifest.previous_manifest_sha256 != expected.manifest.manifest_sha256
            or manifest.expected_pointer_sha256 != expected.pointer_sha256
        ):
            raise BridgeStorageError("bridge orphan parent lineage mismatch")

    @staticmethod
    def _verify_successor_semantics(
        *,
        parent_pointer: BridgeCurrentPointerV1,
        parent_manifest: BridgeTerminalManifestV1,
        parent_artifacts: Mapping[str, bytes],
        child_pointer: BridgeCurrentPointerV1,
        child_manifest: BridgeTerminalManifestV1,
        child_artifacts: Mapping[str, bytes],
    ) -> None:
        if parent_pointer.status not in {"approval_required", "in_progress"}:
            raise BridgeStorageError("terminal bridge revision cannot have a successor")
        if (
            child_pointer.revision != parent_pointer.revision + 1
            or child_manifest.previous_manifest_sha256 != parent_manifest.manifest_sha256
            or child_manifest.expected_pointer_sha256
            != sha256_bytes(canonical_json_bytes(parent_pointer))
        ):
            raise BridgeStorageError("bridge successor parent lineage mismatch")
        parent_inventory = {item.logical_name: item for item in parent_manifest.inventory}
        child_inventory = {item.logical_name: item for item in child_manifest.inventory}
        if not parent_inventory.keys() <= child_inventory.keys():
            raise BridgeStorageError("bridge successor inventory rolled back an artifact")
        for logical_name, parent_entry in parent_inventory.items():
            if (
                child_inventory[logical_name] != parent_entry
                or child_artifacts[logical_name] != parent_artifacts[logical_name]
            ):
                raise BridgeStorageError("bridge successor mutated an immutable artifact")

    def _publish_pointer(
        self,
        *,
        control: Path,
        current: Path,
        pointer: BridgeCurrentPointerV1,
    ) -> None:
        pointer_bytes = canonical_json_bytes(pointer)
        temporary = control / f"c.{pointer.transaction_id}.tmp"
        if temporary.exists():
            if _read_bounded(temporary) != pointer_bytes:
                raise BridgeStorageError("bridge temporary pointer binding mismatch")
        else:
            _write_exclusive(temporary, pointer_bytes)
        self._fault("codex_bridge.publish.current.before_replace")
        os.replace(temporary, current)
        self._fault("codex_bridge.publish.current.after_replace")
        sync_directory(control, hook="codex_bridge.publish.current")

    def _adopt_orphan(
        self,
        *,
        bridge_run_id: str,
        control: Path,
        revisions: Path,
        current: Path,
        prepared: _PreparedPublication,
        expected: VerifiedBridgeRead | None,
    ) -> BridgePublishOutcome | None:
        candidates = self._revision_candidates(revisions, prepared.pointer.revision)
        if not candidates:
            return None
        if len(candidates) != 1:
            raise BridgeStorageError("bridge orphan revision is ambiguous")
        pointer, manifest, _marker, artifacts = self._pointer_for_revision(
            bridge_run_id, candidates[0]
        )
        self._verify_direct_child(pointer, manifest, expected)
        if expected is not None:
            self._verify_successor_semantics(
                parent_pointer=expected.pointer,
                parent_manifest=expected.manifest,
                parent_artifacts=expected.artifacts,
                child_pointer=pointer,
                child_manifest=manifest,
                child_artifacts=artifacts,
            )
        self._publish_pointer(control=control, current=current, pointer=pointer)
        verified = self.read_current(bridge_run_id)
        if verified.pointer != pointer:
            raise BridgeStorageError("adopted bridge pointer did not replay")
        intended = prepared.manifest
        same_logical_update = (
            manifest.bridge_run_id == intended.bridge_run_id
            and manifest.auth_mode is intended.auth_mode
            and manifest.revision == intended.revision
            and manifest.previous_manifest_sha256 == intended.previous_manifest_sha256
            and manifest.expected_pointer_sha256 == intended.expected_pointer_sha256
            and manifest.status == intended.status
            and manifest.runtime_policy_sha256 == intended.runtime_policy_sha256
            and manifest.source_terminal_manifest_sha256 == intended.source_terminal_manifest_sha256
            and manifest.run_plan_sha256 == intended.run_plan_sha256
            and manifest.created_at == intended.created_at
            and manifest.inventory == intended.inventory
            and manifest.inventory_sha256 == intended.inventory_sha256
            and (pointer.completion_marker_sha256 is None)
            == (prepared.pointer.completion_marker_sha256 is None)
        )
        if not same_logical_update:
            raise BridgeStorageError(
                "bridge orphan was reconciled; publication must be retried from current"
            )
        return self._outcome(pointer)

    def publish(self, request: BridgePublishRequest) -> BridgePublishOutcome:
        prepared = self._prepare(request)
        run_id = request.run_plan.bridge_run_id
        with self._authority(run_id):
            self._verify_namespace(run_id)
            _run, control, transactions, revisions, current = self._paths(run_id)
            expected: VerifiedBridgeRead | None = None
            if current.exists():
                expected = self.read_current(run_id)
            if request.expected_pointer_sha256 is None:
                if expected is not None:
                    raise BridgeStorageError("initial bridge publication lost CAS")
            else:
                if (
                    expected is None
                    or expected.pointer.revision != request.expected_revision
                    or expected.manifest.manifest_sha256 != request.expected_manifest_sha256
                    or expected.pointer_sha256 != request.expected_pointer_sha256
                ):
                    raise BridgeStorageError("bridge successor publication lost CAS")
            if expected is not None:
                self._verify_successor_semantics(
                    parent_pointer=expected.pointer,
                    parent_manifest=expected.manifest,
                    parent_artifacts=expected.artifacts,
                    child_pointer=prepared.pointer,
                    child_manifest=prepared.manifest,
                    child_artifacts=prepared.artifact_bytes,
                )
            _verify_execution_progression(
                prepared.artifact_bytes,
                status=prepared.pointer.status,
                completion_marker_present=prepared.completion_marker is not None,
            )
            adopted = self._adopt_orphan(
                bridge_run_id=run_id,
                control=control,
                revisions=revisions,
                current=current,
                prepared=prepared,
                expected=expected,
            )
            if adopted is not None:
                return adopted
            staging = transactions / request.transaction_id
            revision = revisions / f"r{request.proposed_revision}-{request.transaction_id}"
            if staging.exists() or revision.exists():
                raise BridgeStorageError("bridge transaction namespace collision")
            staging.mkdir()
            payload_root = staging / "payload"
            payload_root.mkdir()
            for entry in prepared.manifest.inventory:
                destination = payload_root / Path(entry.logical_name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_exclusive(destination, prepared.artifact_bytes[entry.logical_name])
                sync_directory(
                    destination.parent,
                    hook="codex_bridge.publish.payload_parent",
                )
            _write_exclusive(staging / "manifest.json", prepared.manifest_bytes)
            if prepared.completion_marker_bytes is not None:
                _write_exclusive(staging / "completion.json", prepared.completion_marker_bytes)
            sync_directory(payload_root, hook="codex_bridge.publish.payload_root")
            sync_directory(staging, hook="codex_bridge.publish.staging")
            staging.replace(revision)
            self._fault("codex_bridge.publish.revision.after_rename")
            sync_directory(revisions, hook="codex_bridge.publish.revisions")
            self._read_revision(run_id, prepared.pointer)
            self._publish_pointer(control=control, current=current, pointer=prepared.pointer)
            verified = self.read_current(run_id)
            if verified.pointer != prepared.pointer:
                raise BridgeStorageError("published bridge pointer did not replay")
        return self._outcome(prepared.pointer)

    def _read_revision(
        self,
        bridge_run_id: str,
        pointer: BridgeCurrentPointerV1,
    ) -> tuple[
        BridgeTerminalManifestV1,
        bytes,
        BridgeCompletionMarkerV1 | None,
        bytes | None,
        dict[str, bytes],
    ]:
        _run, _control, _transactions, revisions, _current = self._paths(bridge_run_id)
        revision = revisions / f"r{pointer.revision}-{pointer.transaction_id}"
        verify_directory(revision)
        expected_members = {"manifest.json", "payload"}
        if pointer.completion_marker_sha256 is not None:
            expected_members.add("completion.json")
        if set(item.name for item in revision.iterdir()) != expected_members:
            raise BridgeStorageError("bridge revision membership mismatch")
        manifest_bytes = _read_bounded(revision / "manifest.json")
        manifest = parse_canonical_model(manifest_bytes, BridgeTerminalManifestV1)
        if (
            manifest.bridge_run_id != bridge_run_id
            or manifest.auth_mode is not pointer.auth_mode
            or manifest.revision != pointer.revision
            or manifest.transaction_id != pointer.transaction_id
            or manifest.status != pointer.status
            or manifest.manifest_sha256 != pointer.manifest_sha256
            or manifest.inventory_sha256 != pointer.inventory_sha256
        ):
            raise BridgeStorageError("bridge pointer and manifest disagree")
        payload_root = revision / "payload"
        verify_directory(payload_root)
        expected = {item.logical_name: item for item in manifest.inventory}
        artifacts: dict[str, bytes] = {}
        for path in payload_root.rglob("*"):
            if path.is_dir():
                verify_directory(path)
                continue
            relative = path.relative_to(payload_root).as_posix()
            if relative not in expected:
                raise BridgeStorageError("unexpected bridge payload")
            data = _read_bounded(path)
            entry = expected[relative]
            if len(data) != entry.size_bytes or sha256_bytes(data) != entry.sha256:
                raise BridgeStorageError("bridge payload hash or size mismatch")
            parse_canonical_model(data, _ARTIFACT_MODELS[entry.artifact_kind])
            artifacts[relative] = data
        if set(artifacts) != set(expected):
            raise BridgeStorageError("bridge payload inventory is incomplete")
        replayed_plan = parse_canonical_model(artifacts["run_plan.json"], BridgeRunPlanV1)
        replayed_source = parse_canonical_model(
            artifacts["source_context.json"],
            BridgeSourceContextV1,
        )
        if (
            replayed_plan.bridge_run_id != bridge_run_id
            or replayed_plan.auth_mode is not manifest.auth_mode
            or replayed_plan.runtime_policy_sha256 != manifest.runtime_policy_sha256
            or replayed_plan.plan_sha256 != manifest.run_plan_sha256
            or replayed_plan.source != replayed_source.source
            or replayed_plan.source.source_terminal_manifest_sha256
            != manifest.source_terminal_manifest_sha256
            or replayed_plan.created_at != manifest.created_at
        ):
            raise BridgeStorageError("terminal manifest immutable anchor binding mismatch")
        marker: BridgeCompletionMarkerV1 | None = None
        marker_bytes: bytes | None = None
        if pointer.completion_marker_sha256 is not None:
            marker_bytes = _read_bounded(revision / "completion.json")
            if sha256_bytes(marker_bytes) != pointer.completion_marker_sha256:
                raise BridgeStorageError("bridge completion marker hash mismatch")
            marker = parse_canonical_model(marker_bytes, BridgeCompletionMarkerV1)
            if (
                marker.bridge_run_id != bridge_run_id
                or marker.auth_mode is not pointer.auth_mode
                or marker.terminal_revision != pointer.revision
                or marker.terminal_transaction_id != pointer.transaction_id
                or marker.terminal_status != pointer.status
                or marker.terminal_manifest_sha256 != pointer.manifest_sha256
                or marker.inventory_sha256 != pointer.inventory_sha256
            ):
                raise BridgeStorageError("bridge completion marker correlation mismatch")
        elif pointer.status not in {"approval_required", "in_progress"}:
            raise BridgeStorageError("terminal bridge pointer lacks a completion marker")
        _verify_execution_progression(
            artifacts,
            status=pointer.status,
            completion_marker_present=marker is not None,
        )
        return manifest, manifest_bytes, marker, marker_bytes, artifacts

    def read_current(self, bridge_run_id: str) -> VerifiedBridgeRead:
        _run, _control, _transactions, revisions, current = self._paths(bridge_run_id)
        self._verify_namespace(bridge_run_id)
        pointer_bytes = _read_bounded(current)
        pointer = parse_canonical_model(pointer_bytes, BridgeCurrentPointerV1)
        if pointer.bridge_run_id != bridge_run_id:
            raise BridgeStorageError("bridge current pointer is cross-run")
        manifest, manifest_bytes, marker, marker_bytes, artifacts = self._read_revision(
            bridge_run_id, pointer
        )
        child_pointer = pointer
        child_manifest = manifest
        child_artifacts = artifacts
        seen_revisions: set[int] = {pointer.revision}
        for ordinal in range(pointer.revision - 1, 0, -1):
            candidates = [
                path
                for path in revisions.iterdir()
                if (match := _REVISION_DIRECTORY.fullmatch(path.name)) is not None
                and int(match.group("revision")) == ordinal
            ]
            if len(candidates) != 1:
                raise BridgeStorageError("bridge revision lineage is missing or ambiguous")
            prior_manifest_bytes = _read_bounded(candidates[0] / "manifest.json")
            prior = parse_canonical_model(prior_manifest_bytes, BridgeTerminalManifestV1)
            prior_pointer = BridgeCurrentPointerV1(
                bridge_run_id=prior.bridge_run_id,
                auth_mode=prior.auth_mode,
                revision=prior.revision,
                transaction_id=prior.transaction_id,
                status=prior.status,
                manifest_sha256=prior.manifest_sha256,
                inventory_sha256=prior.inventory_sha256,
                completion_marker_sha256=(
                    sha256_bytes(_read_bounded(candidates[0] / "completion.json"))
                    if (candidates[0] / "completion.json").exists()
                    else None
                ),
                published_at=prior.published_at,
            )
            if (
                prior.bridge_run_id != bridge_run_id
                or prior.auth_mode is not pointer.auth_mode
                or prior.revision in seen_revisions
                or child_manifest.previous_manifest_sha256 != prior.manifest_sha256
                or child_manifest.expected_pointer_sha256
                != sha256_bytes(canonical_json_bytes(prior_pointer))
            ):
                raise BridgeStorageError("bridge revision lineage hash mismatch")
            (
                _prior_manifest,
                _prior_manifest_bytes,
                _prior_marker,
                _prior_marker_bytes,
                prior_artifacts,
            ) = self._read_revision(bridge_run_id, prior_pointer)
            self._verify_successor_semantics(
                parent_pointer=prior_pointer,
                parent_manifest=prior,
                parent_artifacts=prior_artifacts,
                child_pointer=child_pointer,
                child_manifest=child_manifest,
                child_artifacts=child_artifacts,
            )
            seen_revisions.add(prior.revision)
            child_pointer = prior_pointer
            child_manifest = prior
            child_artifacts = prior_artifacts
        self._verify_current_identity_claims(manifest, artifacts)
        self._verify_current_confirmation_identifier_claims(manifest, artifacts)
        return VerifiedBridgeRead(
            pointer=pointer,
            pointer_sha256=sha256_bytes(pointer_bytes),
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            completion_marker=marker,
            completion_marker_bytes=marker_bytes,
            artifacts=artifacts,
        )


__all__ = [
    "BoundedCodexBridgeStore",
    "BridgeExecutionIdentityCollisionError",
    "BridgePublishOutcome",
    "BridgePublishRequest",
    "BridgeStorageError",
    "BridgeStoredArtifact",
    "VerifiedBridgeRead",
]
