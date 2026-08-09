"""Product-facing bounded bridge operations used by the CLI."""

from __future__ import annotations

import base64
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from poker_deliberation.codex_bridge.canonical import sha256_bytes
from poker_deliberation.codex_bridge.contracts import outbound_request_bytes
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.identity import (
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from poker_deliberation.codex_bridge.models import (
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeRole,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import BridgeReplayResult, replay_bridge
from poker_deliberation.codex_bridge.runtime_scratch import (
    PreparedRuntimeRoot,
    RuntimeScratchIdentityError,
)
from poker_deliberation.codex_bridge.sdk_transport import OpenAIAPITransport
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.codex_bridge.subscription_transport import CodexSubscriptionCliTransport
from poker_deliberation.codex_bridge.transport import BridgeTransport
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider

_GIT_ENVIRONMENT_NAMES = (
    "COMSPEC",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


class BridgeProductError(ValueError):
    """Raised when a product operation would escape the bounded bridge contract."""


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    path: Path
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int
    file_attributes: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class _AuthorizedGitignore:
    relative: str
    candidate_blob_id: str
    candidate_bytes: bytes
    working_file: _FileSnapshot


def _is_link_or_reparse(status: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(file_attributes & reparse_flag)


def _require_plain_path(path: Path, repository: Path) -> None:
    """Reject existing link/reparse components without traversing their targets."""

    relative = path.relative_to(repository)
    current = repository
    for part in relative.parts:
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise BridgeProductError("runtime scratch path inspection failed") from exc
        if _is_link_or_reparse(status):
            raise BridgeProductError("runtime scratch path contains a link or reparse point")
        if current != path and not stat.S_ISDIR(status.st_mode):
            raise BridgeProductError("runtime scratch path traverses a non-directory")
        if current == path and not stat.S_ISDIR(status.st_mode):
            raise BridgeProductError("runtime scratch root is not a directory")


def _file_snapshot(path: Path, label: str) -> _FileSnapshot:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or _is_link_or_reparse(status):
            raise BridgeProductError(f"runtime scratch {label} is not a plain file")
        data = path.read_bytes()
        after = path.lstat()
    except BridgeProductError:
        raise
    except OSError as exc:
        raise BridgeProductError(f"runtime scratch {label} inspection failed") from exc
    before_identity = (
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_dev,
        status.st_ino,
        getattr(status, "st_file_attributes", 0),
    )
    after_identity = (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        after.st_dev,
        after.st_ino,
        getattr(after, "st_file_attributes", 0),
    )
    if before_identity != after_identity:
        raise BridgeProductError(f"runtime scratch {label} changed while inspected")
    return _FileSnapshot(
        path=path,
        size=status.st_size,
        modified_ns=status.st_mtime_ns,
        changed_ns=status.st_ctime_ns,
        device=status.st_dev,
        inode=status.st_ino,
        file_attributes=getattr(status, "st_file_attributes", 0),
        content_sha256=sha256_bytes(data),
    )


def _runtime_git(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a bounded Git metadata probe without ambient Git configuration."""

    environment = {name: os.environ[name] for name in _GIT_ENVIRONMENT_NAMES if name in os.environ}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    try:
        return subprocess.run(
            (
                "git",
                "-c",
                f"core.excludesFile={os.devnull}",
                "-c",
                f"safe.directory={repository}",
                "-C",
                str(repository),
                *arguments,
            ),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            env=environment,
        )
    except Exception as exc:
        raise BridgeProductError("runtime scratch Git policy probe failed") from exc


def _nul_paths(data: bytes) -> tuple[str, ...]:
    if data and not data.endswith(b"\0"):
        raise BridgeProductError("runtime scratch Git policy output is malformed")
    try:
        return tuple(item.decode("utf-8", errors="strict") for item in data.split(b"\0") if item)
    except UnicodeDecodeError as exc:
        raise BridgeProductError("runtime scratch Git policy output is malformed") from exc


def _tracked_runtime_paths(repository: Path) -> frozenset[str]:
    completed = _runtime_git(repository, ("ls-files", "-z"))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch Git index probe failed")
    return frozenset(path.replace("\\", "/") for path in _nul_paths(completed.stdout))


def _git_object_id(data: bytes, label: str) -> str:
    try:
        value = data.decode("ascii", errors="strict").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError as exc:
        raise BridgeProductError(f"runtime scratch {label} output is malformed") from exc
    if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise BridgeProductError(f"runtime scratch {label} output is malformed")
    return value


def _fixed_candidate_commit(repository: Path) -> str:
    completed = _runtime_git(repository, ("rev-parse", "--verify", "HEAD^{commit}"))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch candidate commit probe failed")
    return _git_object_id(completed.stdout, "candidate commit")


def _fixed_candidate_tree(repository: Path, candidate_commit: str) -> str:
    completed = _runtime_git(
        repository,
        ("rev-parse", "--verify", f"{candidate_commit}^{{tree}}"),
    )
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch candidate tree probe failed")
    return _git_object_id(completed.stdout, "candidate tree")


def _candidate_tracked_runtime_paths(
    repository: Path,
    candidate_tree: str,
) -> frozenset[str]:
    completed = _runtime_git(repository, ("ls-tree", "-r", "--name-only", "-z", candidate_tree))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch candidate path probe failed")
    return frozenset(path.replace("\\", "/") for path in _nul_paths(completed.stdout))


def _fixed_blob_bytes(repository: Path, object_id: str) -> bytes:
    completed = _runtime_git(repository, ("cat-file", "blob", object_id))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch candidate blob probe failed")
    return completed.stdout


def _git_index_snapshot(repository: Path) -> _FileSnapshot:
    completed = _runtime_git(repository, ("rev-parse", "--git-path", "index"))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch Git index path probe failed")
    try:
        raw_path = completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BridgeProductError("runtime scratch Git index path output is malformed") from exc
    if not raw_path:
        raise BridgeProductError("runtime scratch Git index path output is malformed")
    index_path = Path(raw_path)
    if not index_path.is_absolute():
        index_path = repository / index_path
    return _file_snapshot(index_path, "Git index")


def _literal_pathspec(relative: str) -> str:
    return f":(literal){relative}"


def _tree_entry(
    repository: Path,
    candidate_tree: str,
    relative: str,
) -> tuple[str, str] | None:
    completed = _runtime_git(
        repository,
        ("ls-tree", "-z", candidate_tree, "--", _literal_pathspec(relative)),
    )
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch candidate tree entry probe failed")
    entries = _nul_paths(completed.stdout)
    if not entries:
        return None
    if len(entries) != 1:
        raise BridgeProductError("runtime scratch candidate tree entry is malformed")
    metadata, separator, reported_path = entries[0].partition("\t")
    fields = metadata.split(" ")
    if (
        separator != "\t"
        or reported_path != relative
        or len(fields) != 3
        or fields[1] != "blob"
        or fields[0] not in {"100644", "100755"}
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2]) is None
    ):
        raise BridgeProductError("runtime scratch candidate tree entry is malformed")
    return fields[0], fields[2]


def _index_entry(repository: Path, relative: str) -> tuple[str, str] | None:
    pathspec = _literal_pathspec(relative)
    completed = _runtime_git(repository, ("ls-files", "--stage", "-z", "--", pathspec))
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch Git index entry probe failed")
    entries = _nul_paths(completed.stdout)
    if not entries:
        return None
    if len(entries) != 1:
        raise BridgeProductError("runtime scratch Git index entry is malformed")
    metadata, separator, reported_path = entries[0].partition("\t")
    fields = metadata.split(" ")
    if (
        separator != "\t"
        or reported_path != relative
        or len(fields) != 3
        or fields[2] != "0"
        or fields[0] not in {"100644", "100755"}
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[1]) is None
    ):
        raise BridgeProductError("runtime scratch Git index entry is malformed")
    flags = _runtime_git(repository, ("ls-files", "-v", "-z", "--", pathspec))
    if flags.returncode != 0 or _nul_paths(flags.stdout) != (f"H {relative}",):
        raise BridgeProductError("runtime scratch .gitignore has unsafe index flags")
    return fields[0], fields[1]


def _relevant_gitignore_paths(relative: str) -> tuple[str, ...]:
    parts = Path(relative).parts
    return tuple(
        "/".join((*parts[:depth], ".gitignore")) if depth else ".gitignore"
        for depth in range(len(parts) + 1)
    )


def _authorized_runtime_gitignores(
    repository: Path,
    relative: str,
    candidate_tree: str,
) -> tuple[_AuthorizedGitignore, ...]:
    authorized: list[_AuthorizedGitignore] = []
    for source_relative in _relevant_gitignore_paths(relative):
        source = repository / source_relative
        tree_entry = _tree_entry(repository, candidate_tree, source_relative)
        index_entry = _index_entry(repository, source_relative)
        try:
            source_status = source.lstat()
        except FileNotFoundError:
            source_status = None
        except OSError as exc:
            raise BridgeProductError("runtime scratch .gitignore inspection failed") from exc
        if tree_entry is None and index_entry is None and source_status is None:
            continue
        if (
            tree_entry is None
            or index_entry is None
            or source_status is None
            or tree_entry != index_entry
            or not stat.S_ISREG(source_status.st_mode)
            or _is_link_or_reparse(source_status)
        ):
            raise BridgeProductError("runtime scratch .gitignore authority is not clean")
        try:
            _require_plain_path(source.parent, repository)
        except BridgeProductError as exc:
            raise BridgeProductError("runtime scratch .gitignore authority is not plain") from exc
        hashed = _runtime_git(repository, ("hash-object", "--no-filters", "--", source_relative))
        if (
            hashed.returncode != 0
            or _git_object_id(hashed.stdout, ".gitignore hash") != tree_entry[1]
        ):
            raise BridgeProductError("runtime scratch .gitignore bytes are not clean")
        candidate_bytes = _fixed_blob_bytes(repository, tree_entry[1])
        working_file = _file_snapshot(source, ".gitignore authority")
        if sha256_bytes(candidate_bytes) != working_file.content_sha256:
            raise BridgeProductError("runtime scratch .gitignore bytes are not clean")
        authorized.append(
            _AuthorizedGitignore(
                relative=source_relative,
                candidate_blob_id=tree_entry[1],
                candidate_bytes=candidate_bytes,
                working_file=working_file,
            )
        )
    return tuple(authorized)


def _path_overlaps_tracked(relative: str, tracked_paths: frozenset[str]) -> bool:
    folded = relative.casefold()
    prefix = f"{folded}/"
    for tracked in tracked_paths:
        tracked_folded = tracked.casefold()
        if (
            tracked_folded == folded
            or tracked_folded.startswith(prefix)
            or folded.startswith(f"{tracked_folded}/")
        ):
            return True
    return False


def _ignored_by_fixed_gitignore(
    repository: Path,
    relative: str,
    authorized_gitignores: tuple[_AuthorizedGitignore, ...],
) -> bool:
    """Evaluate only candidate-tree ignore bytes in a single-use isolated repository."""

    try:
        temporary_context = tempfile.TemporaryDirectory(prefix="poker-deliberation-runtime-ignore-")
    except OSError as exc:
        raise BridgeProductError("runtime scratch isolated ignore probe failed") from exc
    with temporary_context as temporary_name:
        snapshot = Path(temporary_name).resolve(strict=True)
        if snapshot == repository or snapshot.is_relative_to(repository):
            raise BridgeProductError("runtime scratch isolated ignore probe is inside repository")
        initialized = _runtime_git(snapshot, ("init", "-q"))
        if initialized.returncode != 0:
            raise BridgeProductError("runtime scratch isolated ignore probe failed")
        info_exclude = snapshot / ".git" / "info" / "exclude"
        try:
            info_exclude.write_bytes(b"")
            for authority in authorized_gitignores:
                destination = snapshot / authority.relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(authority.candidate_bytes)
            (snapshot / relative).parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BridgeProductError("runtime scratch isolated ignore probe failed") from exc

        directory_probe = f"{relative.rstrip('/')}/".encode() + b"\0"
        completed = _runtime_git(
            snapshot,
            ("check-ignore", "--no-index", "-v", "-z", "--stdin"),
            input_bytes=directory_probe,
        )
    if completed.returncode == 1:
        return False
    if completed.returncode != 0:
        raise BridgeProductError("runtime scratch Git ignore probe failed")
    fields = _nul_paths(completed.stdout)
    if len(fields) != 4:
        raise BridgeProductError("runtime scratch Git ignore output is malformed")
    source, _line_number, _pattern, matched_path = fields
    if matched_path.rstrip("/") != relative.rstrip("/"):
        raise BridgeProductError("runtime scratch Git ignore output is malformed")
    source_path = Path(source)
    if source_path.is_absolute():
        return False
    source_relative = source_path.as_posix()
    authorized_paths = {authority.relative for authority in authorized_gitignores}
    return source_relative in authorized_paths and source_path.name == ".gitignore"


def confined_runtime_scratch_path(path: Path, repository_root: Path) -> Path:
    """Require an untracked repository path ignored by a tracked ``.gitignore``."""

    repository = repository_root.resolve(strict=True)
    if not repository.is_dir():
        raise BridgeProductError("bridge repository root is not a directory")
    raw_parts = tuple(part.casefold() for part in path.parts)
    if ".." in raw_parts or any(part in {".git", "user_materials"} for part in raw_parts):
        raise BridgeProductError("runtime scratch path uses a protected component")
    lexical = Path(os.path.abspath(path))
    try:
        lexical.relative_to(repository)
    except ValueError as exc:
        raise BridgeProductError("bridge path is outside its repository-owned namespace") from exc
    _require_plain_path(lexical, repository)
    resolved = confined_product_path(path, repository)
    top_level = _runtime_git(repository, ("rev-parse", "--show-toplevel"))
    if top_level.returncode != 0:
        raise BridgeProductError("runtime scratch repository probe failed")
    try:
        reported_root = Path(top_level.stdout.decode("utf-8", errors="strict").strip()).resolve(
            strict=True
        )
    except (OSError, UnicodeDecodeError) as exc:
        raise BridgeProductError("runtime scratch repository probe failed") from exc
    if reported_root != repository:
        raise BridgeProductError("runtime scratch repository identity mismatch")
    relative = resolved.relative_to(repository).as_posix()
    candidate_commit = _fixed_candidate_commit(repository)
    candidate_tree = _fixed_candidate_tree(repository, candidate_commit)
    index_before = _git_index_snapshot(repository)
    authorized_gitignores = _authorized_runtime_gitignores(
        repository,
        relative,
        candidate_tree,
    )
    candidate_tracked_paths = _candidate_tracked_runtime_paths(repository, candidate_tree)
    index_tracked_paths = _tracked_runtime_paths(repository)
    if _path_overlaps_tracked(
        relative,
        candidate_tracked_paths | index_tracked_paths,
    ):
        raise BridgeProductError("runtime scratch root overlaps a tracked path")
    if not _ignored_by_fixed_gitignore(repository, relative, authorized_gitignores):
        raise BridgeProductError(
            "runtime scratch root is not ignored by a tracked repository .gitignore"
        )
    if (
        _fixed_candidate_commit(repository) != candidate_commit
        or _fixed_candidate_tree(repository, candidate_commit) != candidate_tree
        or _git_index_snapshot(repository) != index_before
        or _authorized_runtime_gitignores(repository, relative, candidate_tree)
        != authorized_gitignores
        or _candidate_tracked_runtime_paths(repository, candidate_tree) != candidate_tracked_paths
        or _tracked_runtime_paths(repository) != index_tracked_paths
    ):
        raise BridgeProductError("runtime scratch .gitignore authority changed during validation")
    _require_plain_path(lexical, repository)
    final_resolved = confined_product_path(path, repository)
    if final_resolved != resolved:
        raise BridgeProductError("runtime scratch path changed during validation")
    return final_resolved


def _prepare_runtime_scratch_path(path: Path, repository_root: Path) -> PreparedRuntimeRoot:
    """Revalidate and exclusively create a single-use runtime filesystem capability."""

    repository = repository_root.resolve(strict=True)
    confined = confined_runtime_scratch_path(path, repository)
    try:
        return PreparedRuntimeRoot.create(confined, repository)
    except RuntimeScratchIdentityError as exc:
        raise BridgeProductError("runtime scratch root preparation failed") from exc


def confined_product_path(path: Path, repository_root: Path) -> Path:
    """Resolve a bridge-owned path without permitting repository metadata/user data writes."""

    repository = repository_root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if (
        resolved == repository
        or repository not in resolved.parents
        or any(
            part.casefold() in {".git", "user_materials"}
            for part in resolved.relative_to(repository).parts
        )
    ):
        raise BridgeProductError("bridge path is outside its repository-owned namespace")
    return resolved


def _require_disjoint(path: Path, protected: tuple[Path, ...], label: str) -> None:
    if any(path == other or path in other.parents or other in path.parents for other in protected):
        raise BridgeProductError(f"{label} overlaps protected product storage")


def _verified_source(
    config: AppConfig,
    source_run_id: str,
) -> tuple[BridgeSourceContextV1, str]:
    orchestrator = Orchestrator(config=config, provider=LocalProvider())
    read = orchestrator.product_store.read_current(source_run_id)
    source = project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )
    return source, read.manifest_sha256


def prepare_product_bridge(
    *,
    config: AppConfig,
    repository_root: Path,
    bridge_root: Path,
    source_run_id: str,
    bridge_run_id: str,
    repository_commit_id: str,
    repository_tree_id: str,
    auth_mode: RuntimeAuthModeV1,
    api_max_cost_micro_usd: int | None = None,
) -> VerifiedBridgeRead:
    repository = repository_root.resolve(strict=True)
    verify_bridge_checkout(
        repository,
        repository_commit_id=repository_commit_id,
        repository_tree_id=repository_tree_id,
    )
    verify_bridge_module_origins(repository)
    root = confined_product_path(bridge_root, repository_root)
    _require_disjoint(root, config.resolved_storage_roots(), "bridge storage")
    source, _manifest = _verified_source(config, source_run_id)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    return controller.prepare_run(
        bridge_run_id=bridge_run_id,
        source_context=source,
        repository_root=repository,
        repository_commit_id=repository_commit_id,
        repository_tree_id=repository_tree_id,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
    )


def role_request_preview(request: BoundedCodexBridgeRequestV1) -> dict[str, object]:
    outbound = outbound_request_bytes(request)
    if sha256_bytes(outbound) != request.request_bytes_sha256:
        raise BridgeProductError("outbound preview hash mismatch")
    assignment = request.context.assignment
    policy = request.context.runtime_policy
    return {
        "schema_version": "1.0.0",
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode,
        "role": assignment.role,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "parent_assignment_ids": assignment.parent_assignment_ids,
        "expires_at": assignment.expires_at.isoformat().replace("+00:00", "Z"),
        "request_sha256": request.request_sha256,
        "request_bytes_sha256": request.request_bytes_sha256,
        "outbound_scope": "application_owned_canonical_stdin",
        "outbound_bytes": len(outbound),
        "outbound_utf8": outbound.decode("utf-8"),
        "outbound_base64": base64.b64encode(outbound).decode("ascii"),
        "envelope_sha256": request.context.envelope_sha256,
        "runtime_policy_sha256": policy.policy_sha256,
        "runtime_identity": policy.runtime_identity,
        "runtime_binary_sha256": policy.runtime_binary_sha256,
        "model": policy.model,
        "model_provider": policy.model_provider,
        "auth_boundary": (
            "chatgpt"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else ("openai_api_key" if request.auth_mode is RuntimeAuthModeV1.OPENAI_API else "none")
        ),
        "effective_model_identity_status": (
            "UNKNOWN_codex_exec_json_not_exposed"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else "not_applicable_to_confirmation"
        ),
        "actual_backend_model_input_status": (
            "UNKNOWN_codex_exec_json_not_exposed"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else "not_applicable_to_confirmation"
        ),
        "reasoning_effort": policy.reasoning_effort,
        "service_tier": policy.service_tier,
        "classification": policy.classification,
        "usage_classification": policy.usage_classification,
        "model_processing_authorized": policy.model_processing_authorized,
        "credential_reference": policy.credential_reference,
        "credential_value_access": policy.credential_value_access,
        "trace_policy": policy.trace_policy,
        "remote_retention_policy": policy.remote_retention_policy,
        "tool_allowlist": policy.tool_allowlist,
        "provider_fallback_allowed": policy.provider_fallback_allowed,
        "model_fallback_allowed": policy.model_fallback_allowed,
        "network_allowed": policy.network_allowed,
        "budget": policy.budget.model_dump(mode="json"),
    }


def read_product_request(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    auth_mode: RuntimeAuthModeV1,
) -> BoundedCodexBridgeRequestV1:
    root = confined_product_path(bridge_root, repository_root)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    request = controller.read_role_request(bridge_run_id, role)
    if request.auth_mode is not auth_mode:
        raise BridgeProductError("request auth mode mismatch")
    return request


def confirm_product_role(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    authority_id: str,
    confirmation_id: str,
    idempotency_key: str,
    expected_request_sha256: str,
    expected_request_bytes_sha256: str,
    expected_envelope_sha256: str,
    expected_runtime_policy_sha256: str,
    expected_auth_mode: RuntimeAuthModeV1,
    expected_runtime_identity: str,
    expected_model_provider: str,
    expected_model: str | None,
    expected_credential_reference: str,
    expected_remote_retention_policy: str,
) -> VerifiedBridgeRead:
    root = confined_product_path(bridge_root, repository_root)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    return controller.confirm_role(
        bridge_run_id,
        role,
        authority=BridgeConfirmationAuthorityV1(
            authority_id=authority_id,
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        expected_request_sha256=expected_request_sha256,
        expected_request_bytes_sha256=expected_request_bytes_sha256,
        expected_envelope_sha256=expected_envelope_sha256,
        expected_runtime_policy_sha256=expected_runtime_policy_sha256,
        expected_auth_mode=expected_auth_mode,
        expected_runtime_identity=expected_runtime_identity,
        expected_model_provider=expected_model_provider,
        expected_model=expected_model,
        expected_credential_reference=expected_credential_reference,
        expected_remote_retention_policy=expected_remote_retention_policy,
    )


def execute_product_role(
    *,
    config: AppConfig,
    repository_root: Path,
    bridge_root: Path,
    runtime_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    auth_mode: RuntimeAuthModeV1,
    codex_binary: Path | None = None,
) -> VerifiedBridgeRead:
    bridge = confined_product_path(bridge_root, repository_root)
    runtime = confined_runtime_scratch_path(runtime_root, repository_root)
    if bridge == runtime or bridge in runtime.parents or runtime in bridge.parents:
        raise BridgeProductError("bridge storage and runtime scratch roots must not overlap")
    protected = config.resolved_storage_roots()
    _require_disjoint(bridge, protected, "bridge storage")
    _require_disjoint(runtime, protected, "runtime scratch")
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(bridge))
    plan = controller.read_run_plan(bridge_run_id)
    if plan.auth_mode is not auth_mode:
        raise BridgeProductError("execution auth mode mismatch")
    if auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        raise BridgeProductError("local_only never launches a model or network transport")
    verify_bridge_checkout(
        repository_root,
        repository_commit_id=plan.repository_commit_id,
        repository_tree_id=plan.repository_tree_id,
    )
    verify_bridge_module_origins(repository_root)
    stored_source = controller.read_source_context(bridge_run_id)
    current_source, current_manifest_sha256 = _verified_source(
        config,
        stored_source.source.source_terminal_run_id,
    )
    if current_source != stored_source:
        raise BridgeProductError("current P3-030C source no longer matches the bridge context")
    transport: BridgeTransport
    if auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
        prepared_runtime = _prepare_runtime_scratch_path(runtime_root, repository_root)
        if prepared_runtime.path != runtime:
            raise BridgeProductError("runtime scratch path changed before preparation")
        if codex_binary is None:
            try:
                from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]
            except ImportError as exc:
                raise BridgeProductError("subscription runtime extra is not installed") from exc
            codex_binary = bundled_codex_path()
        transport = CodexSubscriptionCliTransport(
            runtime,
            codex_binary=codex_binary,
            runtime_capability=prepared_runtime,
        )
    elif auth_mode is RuntimeAuthModeV1.OPENAI_API:
        transport = OpenAIAPITransport(
            runtime,
            repository_root=repository_root,
            repository_commit_id=plan.repository_commit_id,
            repository_tree_id=plan.repository_tree_id,
            runtime_capability_factory=lambda: _prepare_runtime_scratch_path(
                runtime_root,
                repository_root,
            ),
        )
    else:  # pragma: no cover - enum construction rejects unknown modes
        raise BridgeProductError("unknown execution auth mode")
    return controller.execute_confirmed_role(
        bridge_run_id,
        role,
        auth_mode=auth_mode,
        current_source_terminal_manifest_sha256=current_manifest_sha256,
        transport=transport,
    )


def replay_product_bridge(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
) -> BridgeReplayResult:
    root = confined_product_path(bridge_root, repository_root)
    replayed = replay_bridge(BoundedCodexBridgeStore(root).read_current(bridge_run_id))
    if replayed.auth_mode is not auth_mode:
        raise BridgeProductError("terminal replay auth mode mismatch")
    return replayed


def bridge_read_summary(read: VerifiedBridgeRead) -> dict[str, object]:
    replayed = replay_bridge(read)
    return {
        "bridge_run_id": replayed.bridge_run_id,
        "auth_mode": replayed.auth_mode,
        "revision": replayed.revision,
        "status": replayed.status,
        "completed_roles": replayed.completed_roles,
        "pending_roles": replayed.pending_roles,
        "reconciliation_required": replayed.reconciliation_required,
        "total_input_tokens": replayed.total_input_tokens,
        "total_output_tokens": replayed.total_output_tokens,
        "total_estimated_cost_micro_usd": replayed.total_estimated_cost_micro_usd,
        "manifest_sha256": read.manifest.manifest_sha256,
        "inventory_sha256": read.manifest.inventory_sha256,
        "completion_marker_sha256": read.pointer.completion_marker_sha256,
    }


__all__ = [
    "BridgeProductError",
    "bridge_read_summary",
    "confined_product_path",
    "confined_runtime_scratch_path",
    "confirm_product_role",
    "execute_product_role",
    "prepare_product_bridge",
    "read_product_request",
    "replay_product_bridge",
    "role_request_preview",
]
