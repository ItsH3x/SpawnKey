"""XDG-compliant persistent profile storage and volatile runtime state store."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable, Iterator
import unicodedata

from spawnkey.core.models import DropdownProfile, RuntimeState


DEFAULT_RUN_DEBOUNCE_MS = 110
_HELD_LOCK_GUARD = threading.Lock()
_HELD_LOCK_OWNERS: dict[Path, int] = {}


def run_lock_path(paths: "AppPaths", profile_id: str) -> Path:
    """Return per-profile lock file under $XDG_RUNTIME_DIR/spawnkey/<id>.lock."""
    return paths.state_dir / f"{profile_id}.lock"


def run_timestamp_path(paths: "AppPaths", profile_id: str) -> Path:
    """Return per-profile debounce timestamp file under $XDG_RUNTIME_DIR/spawnkey/<id>.ts."""
    return paths.state_dir / f"{profile_id}.ts"


@contextmanager
def profile_run_guard(
    paths: "AppPaths",
    profile_id: str,
    *,
    debounce_ms: int = DEFAULT_RUN_DEBOUNCE_MS,
    non_blocking: bool = True,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[bool]:
    """Acquire non-blocking flock and enforce lightweight timestamp debounce per profile.

    Yields True when the caller may proceed, or False when another invocation already
    holds the lock (when non_blocking=True) or the profile is still within debounce_ms.
    """
    lock_path = run_lock_path(paths, profile_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_lock = lock_path.resolve()
    tid = threading.get_ident()

    with _HELD_LOCK_GUARD:
        if _HELD_LOCK_OWNERS.get(resolved_lock) == tid:
            reentrant = True
        else:
            reentrant = False
    if reentrant:
        yield True
        return

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if non_blocking else 0)
        try:
            fcntl.flock(fd, flags)
            locked = True
        except OSError as exc:
            if non_blocking and exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                yield False
                return
            raise

        ts_path = run_timestamp_path(paths, profile_id)
        if debounce_ms > 0 and ts_path.exists():
            last_ts: float | None = None
            try:
                last_ts = float(ts_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                last_ts = None
            if last_ts is not None:
                elapsed_ms = (clock() - last_ts) * 1000.0
                if 0.0 <= elapsed_ms < float(debounce_ms):
                    yield False
                    return

        with _HELD_LOCK_GUARD:
            _HELD_LOCK_OWNERS[resolved_lock] = tid
        try:
            yield True
        finally:
            if debounce_ms > 0:
                try:
                    ts_path.write_text(f"{clock():.6f}\n", encoding="utf-8")
                except OSError:
                    pass
            with _HELD_LOCK_GUARD:
                if _HELD_LOCK_OWNERS.get(resolved_lock) == tid:
                    _HELD_LOCK_OWNERS.pop(resolved_lock, None)
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


@dataclass(frozen=True)
class AppPaths:
    """Resolved XDG directories used by SpawnKey."""

    config_dir: Path
    data_dir: Path
    runtime_dir: Path

    @property
    def profiles_dir(self) -> Path:
        return self.config_dir / "profiles"

    @property
    def scripts_dir(self) -> Path:
        return self.data_dir / "scripts"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir

    @classmethod
    def from_env(cls) -> "AppPaths":
        home = Path.home()
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config"))
        data_home = Path(os.environ.get("XDG_DATA_HOME") or (home / ".local" / "share"))
        runtime_base = os.environ.get("XDG_RUNTIME_DIR")
        if runtime_base:
            runtime_dir = Path(runtime_base) / "spawnkey"
        else:
            runtime_dir = Path(tempfile.gettempdir()) / f"spawnkey-{os.getuid()}"
        return cls(
            config_dir=config_home / "spawnkey",
            data_dir=data_home / "spawnkey",
            runtime_dir=runtime_dir,
        )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def slugify_id(name: str) -> str:
    """Convert a human-readable dropdown name into a safe profile slug."""
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        slug = "dropdown"
    return slug[:64]


class ProfileStore:
    """CRUD storage for SpawnKey dropdown profiles under XDG_CONFIG_HOME."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.from_env()

    def profile_path(self, profile_id: str) -> Path:
        return self.paths.profiles_dir / f"{profile_id}.json"

    def ensure_unique_id(self, base_name: str) -> str:
        base = slugify_id(base_name)
        candidate = base
        counter = 2
        while self.profile_path(candidate).exists():
            suffix = f"-{counter}"
            candidate = f"{base[: 64 - len(suffix)]}{suffix}"
            counter += 1
        return candidate

    def list_profiles(self) -> list[DropdownProfile]:
        profiles_dir = self.paths.profiles_dir
        if not profiles_dir.exists():
            return []
        result: list[DropdownProfile] = []
        for entry in sorted(profiles_dir.glob("*.json")):
            with entry.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            result.append(DropdownProfile.from_dict(data))
        return result

    def get_profile(self, profile_id: str) -> DropdownProfile | None:
        path = self.profile_path(profile_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return DropdownProfile.from_dict(data)

    def save_profile(self, profile: DropdownProfile) -> Path:
        profile.validate()
        path = self.profile_path(profile.id)
        _atomic_write_json(path, profile.to_dict())
        return path

    def delete_profile(self, profile_id: str) -> bool:
        path = self.profile_path(profile_id)
        if not path.exists():
            return False
        path.unlink()
        return True


class RuntimeStateStore:
    """Volatile runtime state per profile under XDG_RUNTIME_DIR."""

    def __init__(self, paths: AppPaths | None = None) -> None:
        self.paths = paths or AppPaths.from_env()

    def state_path(self, profile_id: str) -> Path:
        return self.paths.state_dir / f"{profile_id}.state.json"

    def load_state(self, profile_id: str) -> RuntimeState:
        path = self.state_path(profile_id)
        if not path.exists():
            return RuntimeState(profile_id=profile_id)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return RuntimeState.from_dict(data)
        except (OSError, ValueError, KeyError):
            return RuntimeState(profile_id=profile_id)

    def save_state(self, state: RuntimeState) -> Path:
        path = self.state_path(state.profile_id)
        _atomic_write_json(path, state.to_dict())
        return path

    def clear_state(self, profile_id: str) -> bool:
        orig_path = self.paths.state_dir / f"{profile_id}.orig.json"
        if orig_path.exists():
            try:
                orig_path.unlink()
            except OSError:
                pass
        path = self.state_path(profile_id)
        if not path.exists():
            return False
        path.unlink()
        return True
