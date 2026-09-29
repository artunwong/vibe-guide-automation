"""Project and user-cache path boundaries."""

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Optional, Union


PathLike = Union[str, Path]


def _canonical(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _nearest_existing_directory(path: Path) -> Path:
    candidate = path
    if candidate.exists() and candidate.is_file():
        candidate = candidate.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if candidate.exists() and candidate.is_file():
        candidate = candidate.parent
    return candidate


def _git_root(start: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return _canonical(Path(result.stdout.strip()))


def _marker_root(start: Path) -> Optional[Path]:
    current = start
    while True:
        for marker in (".project-root", "AGENTS.md", "CODEBUDDY.md", "CLAUDE.md"):
            if (current / marker).exists():
                return _canonical(current)
        if current == current.parent:
            return None
        current = current.parent


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    vibe_home: Optional[Path] = None

    def __post_init__(self):
        root = _canonical(self.root)
        if not root.exists() or not root.is_dir():
            raise ValueError("project root must be an existing directory")
        configured = self.vibe_home
        if configured is None:
            configured = os.environ.get("VIBE_HOME") or str(Path.home() / ".vibe-guide")
        configured = Path(configured).expanduser()
        if not configured.is_absolute():
            raise ValueError("VIBE_HOME must be an absolute path")
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "vibe_home", _canonical(configured))

    @classmethod
    def from_cwd(cls, cwd: Path) -> "ProjectPaths":
        start = _nearest_existing_directory(_canonical(Path(cwd)))
        root = _git_root(start) or _marker_root(start) or start
        return cls(root)

    @property
    def project_root(self) -> Path:
        return self.root

    @property
    def vibe_dir(self) -> Path:
        return self._contained(self.root / ".vibe", self.root)

    @property
    def vibe(self) -> Path:
        return self.vibe_dir

    @property
    def history_dir(self) -> Path:
        """Canonical, project-contained namespace for read-only legacy runs.

        This is deliberately distinct from ``.vibe/runs`` so migration and
        replay can never be mistaken for current execution state.
        """
        return self._contained(self.vibe_dir / "history", self.vibe_dir)

    @property
    def user_home(self) -> Path:
        return self.vibe_home

    def _contained(self, candidate: Path, base: Path) -> Path:
        resolved = _canonical(candidate)
        base_resolved = _canonical(base)
        if not _contains(base_resolved, resolved):
            raise ValueError("path escapes its allowed boundary")
        return resolved

    def resolve_relative(self, relative: PathLike) -> Path:
        value = Path(relative)
        if value.is_absolute():
            raise ValueError("relative path required")
        return self._contained(self.root / value, self.root)

    def resolve_vibe_path(self, relative: PathLike) -> Path:
        value = Path(relative)
        if value.is_absolute():
            raise ValueError("relative path required")
        return self._contained(self.vibe_dir / value, self.vibe_dir)

    def resolve_vibe_home_path(self, relative: PathLike) -> Path:
        value = Path(relative)
        if value.is_absolute():
            raise ValueError("relative path required")
        return self._contained(self.vibe_home / value, self.vibe_home)
