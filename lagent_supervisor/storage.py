from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, TypeVar

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore

T = TypeVar("T")


def _lock_path(path: Path) -> Path:
    suffix = path.suffix + ".lock" if path.suffix else ".lock"
    return path.with_suffix(suffix)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JsonFile:
    @staticmethod
    def load(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    @staticmethod
    def dump(path: Path, data: Any, *, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(path):
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode is not None:
                    os.chmod(tmp_name, mode)
                os.replace(tmp_name, path)
                if mode is not None:
                    os.chmod(path, mode)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def update(path: Path, default: T, mutator: Callable[[T], T], *, mode: int | None = None) -> T:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(path):
            current: T = default
            if path.exists():
                with path.open("r", encoding="utf-8") as handle:
                    current = json.load(handle)
            new_value = mutator(current)
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(new_value, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                if mode is not None:
                    os.chmod(tmp_name, mode)
                os.replace(tmp_name, path)
                if mode is not None:
                    os.chmod(path, mode)
            finally:
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
            return new_value


def append_jsonl(path: Path, record: Dict[str, Any], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(path, mode)


def write_jsonl(path: Path, records: list[Dict[str, Any]], *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(path):
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(tmp_name, mode)
            os.replace(tmp_name, path)
            if mode is not None:
                os.chmod(path, mode)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
