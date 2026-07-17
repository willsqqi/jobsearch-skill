from __future__ import annotations

import csv
import io
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock, Timeout

from jobsearch_skill.errors import (
    SchemaValidationError,
    StorageError,
    StorageValidationError,
)
from jobsearch_skill.schema import SchemaRegistry


class SafeStore:
    """Validate and atomically mutate private structured data."""

    def __init__(self, registry: SchemaRegistry, backup_dir: Path) -> None:
        self.registry = registry
        self.backup_dir = backup_dir
        self._ensure_private_directory(backup_dir)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        if path.exists():
            if not path.is_dir():
                raise StorageError(
                    "storage_directory: private storage directory is unavailable",
                    reason_code="storage_directory",
                )
            return
        missing: list[Path] = []
        candidate = path
        while not candidate.exists():
            missing.append(candidate)
            candidate = candidate.parent
        if not candidate.is_dir():
            raise StorageError(
                "storage_directory: private storage parent is unavailable",
                reason_code="storage_directory",
            )
        try:
            for directory in reversed(missing):
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
        except OSError as error:
            raise StorageError(
                "storage_directory: unable to create private storage directory",
                reason_code="storage_directory",
            ) from error

    @staticmethod
    def _lock_for(path: Path) -> FileLock:
        return FileLock(str(path.with_name(f"{path.name}.lock")), mode=0o600)

    @staticmethod
    def _secure_lock(lock: FileLock) -> None:
        try:
            Path(lock.lock_file).chmod(0o600)
        except OSError as error:
            raise StorageError(
                "storage_lock: unable to secure private storage lock",
                reason_code="storage_lock",
            ) from error

    @contextmanager
    def _locked(self, path: Path):
        lock = self._lock_for(path)
        try:
            lock.acquire()
        except (OSError, Timeout) as error:
            raise StorageError(
                "storage_lock: unable to lock private data",
                reason_code="storage_lock",
            ) from error
        try:
            self._secure_lock(lock)
            yield
        finally:
            try:
                lock.release()
            except (OSError, Timeout) as error:
                raise StorageError(
                    "storage_lock: unable to release private data lock",
                    reason_code="storage_lock",
                ) from error

    def ensure_private_file(self, path: Path) -> None:
        """Normalize an existing private regular file to mode 0600 without rewriting it."""

        with self._locked(path):
            try:
                if path.is_symlink() or not path.is_file():
                    raise StorageError(
                        "storage_mode: private data must be a regular file",
                        reason_code="storage_mode",
                    )
                if stat.S_IMODE(path.stat().st_mode) != 0o600:
                    path.chmod(0o600)
                if stat.S_IMODE(path.stat().st_mode) != 0o600:
                    raise OSError("private file mode normalization failed")
            except StorageError:
                raise
            except OSError as error:
                raise StorageError(
                    "storage_mode: unable to secure private data",
                    reason_code="storage_mode",
                ) from error

    def _validate(self, contract: str, value: object) -> None:
        try:
            self.registry.validate(contract, value)
        except SchemaValidationError as error:
            raise StorageValidationError(
                "storage_validation: replacement violates its contract",
                reason_code="storage_validation",
                field_path=error.field_path,
            ) from error

    def _backup_path(self, path: Path) -> Path:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return self.backup_dir / f"{path.stem}.{timestamp}{path.suffix}"

    def _create_backup(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        backup_path = self._backup_path(path)
        try:
            with path.open("rb") as source:
                descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as destination:
                    shutil.copyfileobj(source, destination)
                    destination.flush()
                    os.fsync(destination.fileno())
            backup_path.chmod(0o600)
        except OSError as error:
            raise StorageError(
                "storage_backup: unable to create private backup",
                reason_code="storage_backup",
            ) from error
        return backup_path

    @staticmethod
    def _write_temp(path: Path, text: str) -> Path:
        temporary_path: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            if stat.S_IMODE(temporary_path.stat().st_mode) != 0o600:
                raise OSError("private temporary file mode is unsafe")
            return temporary_path
        except (OSError, UnicodeError) as error:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise StorageError(
                "storage_write: unable to prepare private replacement",
                reason_code="storage_write",
            ) from error

    def _replace_locked(self, path: Path, text: str, *, create_backup: bool) -> None:
        temporary_path: Path | None = None
        try:
            temporary_path = self._write_temp(path, text)
            if create_backup:
                self._create_backup(path)
            os.replace(temporary_path, path)
            temporary_path = None
        except StorageError:
            raise
        except OSError as error:
            raise StorageError(
                "storage_replace: unable to atomically replace private data",
                reason_code="storage_replace",
            ) from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def write_text(self, path: Path, text: str, validator: Callable[[str], None]) -> None:
        with self._locked(path):
            try:
                validator(text)
            except SchemaValidationError as error:
                raise StorageValidationError(
                    "storage_validation: replacement violates its contract",
                    reason_code="storage_validation",
                    field_path=error.field_path,
                ) from error
            except Exception as error:
                raise StorageError(
                    "storage_validation: replacement validation failed",
                    reason_code="storage_validation",
                ) from error
            self._replace_locked(path, text, create_backup=True)

    def read_yaml(self, path: Path, contract: str) -> dict[str, object]:
        with self._locked(path):
            try:
                value = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                raise StorageError(
                    "storage_read: unable to read private YAML",
                    reason_code="storage_read",
                ) from error
            if not isinstance(value, dict):
                raise StorageValidationError(
                    "storage_validation: private YAML is not an object",
                    reason_code="storage_validation",
                )
            self._validate(contract, value)
            return value

    def write_yaml(
        self, path: Path, value: Mapping[str, object], contract: str
    ) -> None:
        with self._locked(path):
            self._validate(contract, value)
            text = yaml.safe_dump(dict(value), sort_keys=False, allow_unicode=True)
            self._replace_locked(path, text, create_backup=True)

    def migrate_yaml(
        self,
        path: Path,
        source_contract: str,
        target_contract: str,
        transform: Callable[[dict[str, object]], dict[str, object]],
    ) -> None:
        with self._locked(path):
            try:
                source = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, yaml.YAMLError) as error:
                raise StorageError(
                    "storage_read: unable to read migration source",
                    reason_code="storage_read",
                ) from error
            if not isinstance(source, dict):
                raise StorageValidationError(
                    "storage_validation: migration source is not an object",
                    reason_code="storage_validation",
                )
            self._validate(source_contract, source)
            self._create_backup(path)
            try:
                target = transform(source)
            except Exception as error:
                raise StorageError(
                    "storage_transform: migration transform failed",
                    reason_code="storage_transform",
                ) from error
            if not isinstance(target, dict):
                raise StorageValidationError(
                    "storage_validation: migration target is not an object",
                    reason_code="storage_validation",
                )
            self._validate(target_contract, target)
            text = yaml.safe_dump(target, sort_keys=False, allow_unicode=True)
            self._replace_locked(path, text, create_backup=False)

    def read_json(self, path: Path, contract: str) -> dict[str, object]:
        with self._locked(path):
            try:
                value: Any = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise StorageError(
                    "storage_read: unable to read private JSON",
                    reason_code="storage_read",
                ) from error
            if not isinstance(value, dict):
                raise StorageValidationError(
                    "storage_validation: private JSON is not an object",
                    reason_code="storage_validation",
                )
            self._validate(contract, value)
            return value

    def write_json(
        self, path: Path, value: Mapping[str, object], contract: str
    ) -> None:
        with self._locked(path):
            self._validate(contract, value)
            text = json.dumps(dict(value), indent=2, sort_keys=True) + "\n"
            self._replace_locked(path, text, create_backup=True)

    def rewrite_csv(
        self,
        path: Path,
        fieldnames: Sequence[str],
        rows: Sequence[Mapping[str, str]],
        row_contract: str,
        schema_version: int,
    ) -> None:
        with self._locked(path):
            expected_fields = tuple(fieldnames)
            for row in rows:
                if tuple(row.keys()) != expected_fields:
                    raise StorageValidationError(
                        "storage_validation: CSV row fields do not match the versioned header",
                        reason_code="storage_validation",
                    )
                self._validate(row_contract, {"schema_version": schema_version, **row})
            stream = io.StringIO(newline="")
            stream.write(f"# schema_version={schema_version}\n")
            writer = csv.DictWriter(stream, fieldnames=expected_fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            self._replace_locked(path, stream.getvalue(), create_backup=True)
