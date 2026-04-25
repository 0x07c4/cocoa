from __future__ import annotations

import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


DEFAULT_IGNORES = {
    ".git",
    ".cocoa",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "target",
}


@dataclass(frozen=True)
class FileEntry:
    path: str
    size: int


class WorkspaceScope:
    def __init__(self, root: Path, ignores: set[str] | None = None) -> None:
        self.root = root.resolve()
        self.ignores = set(DEFAULT_IGNORES)
        if ignores:
            self.ignores.update(ignores)

    def resolve(self, path: str | Path) -> Path:
        candidate = (self.root / path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes workspace: {path}") from exc
        return candidate

    def is_ignored(self, relative: str) -> bool:
        parts = Path(relative).parts
        for part in parts:
            if part in self.ignores:
                return True
        return any(fnmatch(relative, pattern) for pattern in self.ignores)

    def inspect(self, path: str | Path = ".", max_entries: int = 120) -> list[FileEntry]:
        if max_entries <= 0:
            raise ValueError("max_entries must be greater than 0")
        start = self.resolve(path)
        if not start.exists():
            raise FileNotFoundError(start)

        if start.is_file():
            relative = start.relative_to(self.root).as_posix()
            if self.is_ignored(relative):
                raise ValueError(f"path is ignored: {relative}")
            return [FileEntry(path=relative, size=start.stat().st_size)]

        entries: list[FileEntry] = []
        for current, dirs, files in os.walk(start):
            current_path = Path(current)
            rel_dir = current_path.relative_to(self.root).as_posix()
            dirs[:] = [
                name
                for name in dirs
                if not self.is_ignored(name)
                and not self.is_ignored((current_path / name).relative_to(self.root).as_posix())
            ]
            for name in sorted(files):
                file_path = current_path / name
                relative = file_path.relative_to(self.root).as_posix()
                if self.is_ignored(relative):
                    continue
                entries.append(FileEntry(path=relative, size=file_path.stat().st_size))
                if len(entries) >= max_entries:
                    return entries
            if rel_dir == ".":
                dirs.sort()
        return entries
