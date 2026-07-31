from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

if sys.platform != "win32":
    pytest.skip("Windows Job Object adversarial tests", allow_module_level=True)

from poker_deliberation.isolated_jobs.canonical import canonical_child_argv
from poker_deliberation.isolated_jobs.coordinator import qualify_isolated_job_policy
from poker_deliberation.isolated_jobs.models import (
    FileIdentityV1,
    FilesystemPolicyV1,
    IsolatedJobError,
    IsolatedJobRequestV1,
    JobFailureCode,
    SecretReferenceV1,
    SyntheticOperation,
)
from poker_deliberation.isolated_jobs.paths import _is_reparse, file_identity
from poker_deliberation.isolated_jobs.windows_backend import WindowsJobBackend
from tests.isolated_job_support import limits, policy_for, request

pytestmark = pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows path and Job Object adversarial tests",
)


def test_hardlinked_approved_input_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    alias = workspace / "alias.txt"
    source.write_bytes(b"public fixture\n")
    os.link(source, alias)

    with pytest.raises(IsolatedJobError) as rejected:
        policy_for(workspace, approved_input=alias)

    assert rejected.value.code is JobFailureCode.HARDLINK_DETECTED


def test_windows_reparse_attribute_is_detected_without_link_privilege() -> None:
    class ReparseStat:
        st_file_attributes = 0x400

    assert _is_reparse(ReparseStat()) is True  # type: ignore[arg-type]


def test_symlinked_workspace_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    linked_workspace = tmp_path / "linked-workspace"
    try:
        os.symlink(workspace, linked_workspace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")

    with pytest.raises(IsolatedJobError) as rejected:
        policy_for(linked_workspace)

    assert rejected.value.code is JobFailureCode.LINK_OR_REPARSE_DETECTED


def test_symlinked_approved_input_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "source.txt"
    source.write_bytes(b"public fixture\n")
    linked_input = workspace / "linked-input.txt"
    try:
        os.symlink(source, linked_input)
    except OSError as exc:
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")

    with pytest.raises(IsolatedJobError) as rejected:
        policy_for(workspace, approved_input=linked_input)

    assert rejected.value.code is JobFailureCode.LINK_OR_REPARSE_DETECTED


def test_approved_input_must_remain_beneath_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"public fixture\n")

    with pytest.raises(ValueError, match="beneath"):
        policy_for(workspace, approved_input=outside)


def test_direct_or_unvalidated_policy_cannot_escape_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"public fixture\n")
    baseline = policy_for(workspace)
    outside_identity = file_identity(
        outside,
        sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
        require_single_link=True,
    )

    with pytest.raises(ValidationError, match="beneath"):
        FilesystemPolicyV1(
            workspace_root=baseline.filesystem.workspace_root,
            approved_input=outside_identity,
            input_handle_required=True,
        )

    bypassed_filesystem = FilesystemPolicyV1.model_construct(
        workspace_root=baseline.filesystem.workspace_root,
        approved_input=outside_identity,
        input_handle_required=True,
    )
    bypassed_policy = baseline.model_copy(
        update={"filesystem": bypassed_filesystem},
    )
    value = request(SyntheticOperation.COPY_HANDLES, suffix="direct-policy-escape")
    with pytest.raises(IsolatedJobError) as rejected:
        WindowsJobBackend().prepare(value, bypassed_policy)
    assert rejected.value.code is JobFailureCode.PATH_CONFINEMENT_FAILED


def test_approved_input_has_a_contract_level_size_cap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "oversized.txt"
    source.write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    with pytest.raises(ValidationError, match="bounded input size"):
        FilesystemPolicyV1(
            workspace_root=policy_for(workspace).filesystem.workspace_root,
            approved_input=file_identity(
                source,
                sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                require_single_link=True,
            ),
            input_handle_required=True,
        )


@pytest.mark.parametrize(
    "payload",
    [
        b"api_key=not-allowed\n",
        b"ghp_abcdefghijklmnopqrstuvwxyz012345\n",
        b"canonical\r\nbut-crlf\r\n",
        b"\xef\xbb\xbfstarts-with-bom\n",
    ],
)
def test_noncanonical_or_secret_shaped_input_is_refused_before_launch(
    tmp_path: Path,
    payload: bytes,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "input.txt"
    source.write_bytes(payload)
    value = request(SyntheticOperation.COPY_HANDLES, suffix="bad-input")
    policy = policy_for(workspace, approved_input=source)

    with pytest.raises(ValueError):
        WindowsJobBackend().prepare(value, policy)


def test_changed_execution_identity_is_refused_before_launch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    value = request(suffix="identity-refusal")
    policy = policy_for(workspace)
    helper = policy.execution_identity.synthetic_helper.model_copy(update={"sha256": "f" * 64})
    changed_identity = policy.execution_identity.model_copy(update={"synthetic_helper": helper})
    changed_policy = policy.model_copy(update={"execution_identity": changed_identity})

    with pytest.raises(IsolatedJobError) as rejected:
        WindowsJobBackend().prepare(value, changed_policy)

    assert rejected.value.code is JobFailureCode.IDENTITY_MISMATCH


def test_same_size_approved_input_mutation_is_refused_before_launch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "input.txt"
    source.write_bytes(b"first fixture\n")
    value = request(SyntheticOperation.COPY_HANDLES, suffix="input-mutation")
    policy = policy_for(workspace, approved_input=source)
    source.write_bytes(b"other fixture\n")

    with pytest.raises(IsolatedJobError) as rejected:
        WindowsJobBackend().prepare(value, policy)

    assert rejected.value.code is JobFailureCode.IDENTITY_MISMATCH


def test_request_has_no_generic_command_environment_or_secret_value_surface() -> None:
    baseline = request(suffix="closed-surface")
    payload = baseline.model_dump(mode="python")

    for injected in (
        {"executable": "cmd.exe"},
        {"argv": ("/c", "whoami")},
        {"environment": {"TOKEN": "secret"}},
        {"shell": True},
    ):
        with pytest.raises(ValidationError):
            IsolatedJobRequestV1.model_validate(payload | injected, strict=True)

    reference = SecretReferenceV1(
        reference_id="credential-metadata-only",
        reference_sha256="a" * 64,
        purpose_sha256="b" * 64,
    )
    with pytest.raises(ValidationError):
        SecretReferenceV1.model_validate(
            reference.model_dump(mode="python") | {"value": "secret"},
            strict=True,
        )

    with pytest.raises(ValidationError):
        FileIdentityV1(
            absolute_path="C:\\workspace\\..\\escape.txt",
            size_bytes=1,
            sha256="e" * 64,
            device_id=1,
            file_id=1,
            link_count=1,
            modified_time_ns=1,
        )
    with pytest.raises(ValidationError, match="format characters"):
        FileIdentityV1(
            absolute_path="C:\\workspace\\safe\u202etxt",
            size_bytes=1,
            sha256="e" * 64,
            device_id=1,
            file_id=1,
            link_count=1,
            modified_time_ns=1,
        )


def test_secret_reference_metadata_never_enters_child_argv(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    baseline = request(suffix="secret-reference")
    value = baseline.model_copy(
        update={
            "secret_references": (
                SecretReferenceV1(
                    reference_id="credential-reference",
                    reference_sha256="c" * 64,
                    purpose_sha256="d" * 64,
                ),
            )
        }
    )
    policy = qualify_isolated_job_policy(
        limits(),
        workspace_root=workspace,
    )

    argv = canonical_child_argv(value, policy)

    assert "credential-reference" not in argv
    assert "c" * 64 not in argv
    assert "d" * 64 not in argv
