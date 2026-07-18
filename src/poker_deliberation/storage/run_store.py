"""Confined, auditable run artifact storage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RunStore:
    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
    ) -> None:
        self.root = root.resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_run_bytes = max_run_bytes
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
        (path / ".poker-deliberation-run").write_text("v1\n", encoding="utf-8")
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
            raise ValueError(f"artifact exceeds hard limit {self.max_artifact_bytes} bytes")
        run_dir = self.run_dir(run_id)
        existing_size = path.stat().st_size if path.exists() else 0
        current_size = sum(
            item.stat().st_size
            for item in run_dir.rglob("*")
            if item.is_file() and not item.name.endswith(".tmp")
        )
        if current_size - existing_size + new_size > self.max_run_bytes:
            raise ValueError(f"run artifacts exceed hard limit {self.max_run_bytes} bytes")

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
