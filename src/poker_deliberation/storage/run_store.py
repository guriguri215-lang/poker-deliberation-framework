"""Confined, auditable run artifact storage."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    artifact_table_entry,
    canonical_json_bytes,
    parse_canonical_json,
    validate_logical_name,
)
from poker_deliberation.storage.terminal_canonical import inventory_entry
from poker_deliberation.storage.terminal_models import VerifiedPayloadV2, VerifiedRunReadV2

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StorageBudgetError(BudgetLimitError, ValueError):
    """Typed hard-cap refusal that remains ValueError-compatible for legacy callers."""


class RunStore:
    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
        usage_observer: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_run_bytes = max_run_bytes
        self.usage_observer = usage_observer
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str, *, create: bool = False) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid run_id")
        path = (self.root / run_id).resolve()
        if path.parent != self.root:
            raise ValueError("run path escapes storage root")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def create_run(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        path.mkdir(parents=True, exist_ok=False)
        (path / ".poker-deliberation-run").write_bytes(b"v1\n")
        return path

    def _artifact_path(self, run_id: str, relative: str, *, for_write: bool = True) -> Path:
        run_dir = self.run_dir(run_id, create=for_write)
        if not for_write and not run_dir.is_dir():
            raise FileNotFoundError(f"run does not exist: {run_id}")
        path = (run_dir / relative).resolve()
        if run_dir not in path.parents:
            raise ValueError("artifact path escapes run directory")
        if for_write:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_directory(self, run_id: str, relative: str) -> Path:
        run_dir = self.run_dir(run_id, create=True)
        path = (run_dir / relative).resolve()
        if path != run_dir and run_dir not in path.parents:
            raise ValueError("directory path escapes run directory")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {str(key): RunStore._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [RunStore._jsonable(item) for item in value]
        return value

    def _enforce_write_budget(self, run_id: str, path: Path, new_size: int) -> None:
        if new_size > self.max_artifact_bytes:
            raise StorageBudgetError(
                BudgetFailure(
                    code=BudgetFailureCode.ARTIFACT_EXCEEDED,
                    resource="artifact_bytes",
                    message=f"artifact exceeds hard limit {self.max_artifact_bytes} bytes",
                    limit=self.max_artifact_bytes,
                    observed=new_size,
                )
            )
        run_dir = self.run_dir(run_id)
        existing_size = path.stat().st_size if path.exists() else 0
        current_size = sum(
            item.stat().st_size
            for item in run_dir.rglob("*")
            if item.is_file() and not item.name.endswith(".tmp")
        )
        projected_run_size = current_size - existing_size + new_size
        if projected_run_size > self.max_run_bytes:
            raise StorageBudgetError(
                BudgetFailure(
                    code=BudgetFailureCode.RUN_EXCEEDED,
                    resource="run_bytes",
                    message=f"run artifacts exceed hard limit {self.max_run_bytes} bytes",
                    limit=self.max_run_bytes,
                    observed=projected_run_size,
                )
            )
        if self.usage_observer is not None:
            self.usage_observer(run_id, new_size, projected_run_size)

    def write_json(self, run_id: str, relative: str, value: Any) -> Path:
        path = self._artifact_path(run_id, relative)
        temporary = path.with_suffix(path.suffix + ".tmp")
        serialized = json.dumps(
            self._jsonable(value),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        content = (serialized + "\n").encode("utf-8")
        self._enforce_write_budget(run_id, path, len(content))
        temporary.write_bytes(content)
        temporary.replace(path)
        return path

    def write_text(self, run_id: str, relative: str, value: str) -> Path:
        path = self._artifact_path(run_id, relative)
        temporary = path.with_suffix(path.suffix + ".tmp")
        content = value.encode("utf-8")
        self._enforce_write_budget(run_id, path, len(content))
        temporary.write_bytes(content)
        temporary.replace(path)
        return path

    def append_jsonl(self, run_id: str, relative: str, value: Any) -> Path:
        path = self._artifact_path(run_id, relative)
        line = json.dumps(
            self._jsonable(value), ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        encoded_line = (line + "\n").encode("utf-8")
        current_size = path.stat().st_size if path.exists() else 0
        self._enforce_write_budget(run_id, path, current_size + len(encoded_line))
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
        return path

    def read_json(self, run_id: str, relative: str) -> Any:
        return json.loads(
            self._artifact_path(run_id, relative, for_write=False).read_text(encoding="utf-8")
        )

    def exists(self, run_id: str) -> bool:
        try:
            return self.run_dir(run_id).is_dir()
        except ValueError:
            return False


class BufferedRunStore:
    """RunStore-compatible canonical buffer with no filesystem write effects."""

    def __init__(
        self,
        virtual_root: Path,
        *,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
        usage_observer: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.root = virtual_root.resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_run_bytes = max_run_bytes
        self.usage_observer = usage_observer
        self._payloads: dict[str, dict[str, bytes]] = {}
        self._directories: dict[str, set[str]] = {}

    def run_dir(self, run_id: str, *, create: bool = False) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid run_id")
        if create:
            self._payloads.setdefault(run_id, {})
            self._directories.setdefault(run_id, set())
        return self.root / run_id

    def create_run(self, run_id: str) -> Path:
        path = self.run_dir(run_id)
        if run_id in self._payloads:
            raise FileExistsError(f"run already exists: {run_id}")
        self._payloads[run_id] = {}
        self._directories[run_id] = set()
        return path

    def ensure_directory(self, run_id: str, relative: str) -> Path:
        self.run_dir(run_id, create=True)
        logical = validate_logical_name(relative + "/placeholder").rsplit("/", 1)[0]
        self._directories[run_id].add(logical)
        return self.run_dir(run_id) / Path(logical)

    def _write(self, run_id: str, relative: str, content: bytes) -> Path:
        self.run_dir(run_id, create=True)
        logical = validate_logical_name(relative)
        payloads = self._payloads[run_id]
        current_size = sum(len(value) for value in payloads.values())
        existing_size = len(payloads.get(logical, b""))
        projected_size = current_size - existing_size + len(content)
        if len(content) > self.max_artifact_bytes:
            raise StorageBudgetError(
                BudgetFailure(
                    code=BudgetFailureCode.ARTIFACT_EXCEEDED,
                    resource="artifact_bytes",
                    message=f"artifact exceeds hard limit {self.max_artifact_bytes} bytes",
                    limit=self.max_artifact_bytes,
                    observed=len(content),
                )
            )
        if projected_size > self.max_run_bytes:
            raise StorageBudgetError(
                BudgetFailure(
                    code=BudgetFailureCode.RUN_EXCEEDED,
                    resource="run_bytes",
                    message=f"run artifacts exceed hard limit {self.max_run_bytes} bytes",
                    limit=self.max_run_bytes,
                    observed=projected_size,
                )
            )
        payloads[logical] = bytes(content)
        if self.usage_observer is not None:
            self.usage_observer(run_id, len(content), projected_size)
        return self.run_dir(run_id) / Path(logical)

    def write_json(self, run_id: str, relative: str, value: Any) -> Path:
        return self._write(run_id, relative, canonical_json_bytes(value))

    def write_text(self, run_id: str, relative: str, value: str) -> Path:
        if not isinstance(value, str):
            raise TypeError("text payload must be a string")
        return self._write(run_id, relative, value.encode("utf-8"))

    def append_jsonl(self, run_id: str, relative: str, value: Any) -> Path:
        logical = validate_logical_name(relative)
        existing = self._payloads.get(run_id, {}).get(logical, b"")
        line = canonical_json_bytes(value) + b"\n"
        return self._write(run_id, logical, existing + line)

    def read_json(self, run_id: str, relative: str) -> Any:
        logical = validate_logical_name(relative)
        try:
            data = self._payloads[run_id][logical]
        except KeyError as exc:
            raise FileNotFoundError(f"artifact does not exist: {logical}") from exc
        return parse_canonical_json(data)

    def exists(self, run_id: str) -> bool:
        return bool(RUN_ID_PATTERN.fullmatch(run_id) and run_id in self._payloads)

    def load_verified(self, read: VerifiedRunReadV2) -> None:
        self._payloads[read.run_id] = {
            payload.inventory.logical_name: bytes(payload.exact_bytes)
            for payload in read.payloads
            if payload.inventory.logical_name != "lifecycle_audit.json"
        }
        self._directories[read.run_id] = {
            logical.rsplit("/", 1)[0] for logical in self._payloads[read.run_id] if "/" in logical
        }

    def verified_payloads(self, run_id: str) -> tuple[VerifiedPayloadV2, ...]:
        try:
            payloads = self._payloads[run_id]
        except KeyError as exc:
            raise FileNotFoundError(f"run does not exist: {run_id}") from exc
        verified: list[VerifiedPayloadV2] = []
        for logical_name, data in payloads.items():
            try:
                media_type, serialization, schema, _origin = artifact_table_entry(logical_name)
            except CanonicalStorageError:
                if logical_name != "state.json":
                    raise
                media_type = "application/json"
                serialization = "poker-run-storage-json-v1"
                schema = "poker-workflow-state-artifact-v1"
            verified.append(
                VerifiedPayloadV2(
                    inventory=inventory_entry(
                        logical_name=logical_name,
                        data=data,
                        media_type=media_type,
                        artifact_schema_version=schema,
                        serialization=serialization,
                    ),
                    exact_bytes=data,
                )
            )
        return tuple(
            sorted(
                verified,
                key=lambda item: item.inventory.revision_relative_path.encode("utf-8"),
            )
        )

    def raw_payload(self, run_id: str, relative: str) -> bytes:
        logical = validate_logical_name(relative)
        try:
            return bytes(self._payloads[run_id][logical])
        except KeyError as exc:
            raise FileNotFoundError(f"artifact does not exist: {logical}") from exc
