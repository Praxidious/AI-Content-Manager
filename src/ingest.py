"""Read-only VOD discovery and local, atomic processing history."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import SUPPORTED_VIDEO_EXTENSIONS
from .utils import atomic_write_json

LOG = logging.getLogger(__name__)


def resolve_path(value: str | Path, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def video_fingerprint(video: Path, block_size: int = 4 * 1024 * 1024) -> str:
    """Preserve the existing size + first/last-block fingerprint algorithm."""
    size = video.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with video.open("rb") as stream:
        digest.update(stream.read(block_size))
        if size > block_size:
            stream.seek(max(0, size - block_size))
            digest.update(stream.read(block_size))
    return digest.hexdigest()[:12]


@contextmanager
def read_guard(path: Path):
    """On Windows/SMB, deny concurrent writers/deleters while allowing FFmpeg reads."""
    if os.name != "nt":
        with path.open("rb"):
            yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        code = ctypes.get_last_error()
        raise OSError(code, f"Cannot safely read {path}; recording/remux may be active, "
                      "or the share is unavailable or access is denied")
    try:
        yield
    finally:
        close(handle)


def snapshot(path: Path) -> tuple[int, int]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Not a regular video file: {path}")
    return info.st_size, info.st_mtime_ns


@dataclass
class Recording:
    path: Path
    size: int = 0
    mtime_ns: int = 0
    status: str = "RECORDING/UNSTABLE"
    reason: str = ""
    fingerprint: str = ""


def discover(source: Path) -> list[Recording]:
    LOG.info("VOD source: %s", source)
    try:
        paths = list(source.iterdir())
    except OSError as exc:
        LOG.error("Network/source available: NO")
        raise RuntimeError(f"Cannot access VOD source {source}. Check the PC, share name, "
                           "network connection and Windows read permissions.") from exc
    LOG.info("Network/source available: YES")
    recordings = []
    for path in paths:
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                continue
            recordings.append(Recording(path, info.st_size, info.st_mtime_ns))
        except OSError as exc:
            recordings.append(Recording(path, reason=str(exc)))
    recordings.sort(key=lambda item: (item.mtime_ns, item.path.name), reverse=True)
    LOG.info("Found %d supported recordings", len(recordings))
    if recordings:
        LOG.info("Newest recording: %s", recordings[0].path.name)
    return recordings


def inspect_recordings(recordings: list[Recording], registry: "Registry", interval: float,
                       scope: str) -> list[Recording]:
    """Observe all candidates over one shared interval, then fingerprint stable files."""
    before = {}
    for item in recordings:
        if re.search(r"(?:^|[._ -])(tmp|temp|partial|part|remux|remuxing)(?:[._ -]|$)", item.path.name, re.I):
            item.reason = "Temporary/remux filename"
            continue
        try:
            with read_guard(item.path):
                before[item.path] = snapshot(item.path)
        except OSError as exc:
            item.reason = str(exc)
    if before:
        LOG.info("Checking recording stability for %g seconds", interval)
        time.sleep(interval)
    for item in recordings:
        if item.path not in before:
            continue
        try:
            with read_guard(item.path):
                current = snapshot(item.path)
                if current != before[item.path] or current[0] <= 0 or time.time_ns() - current[1] < interval * 1e9:
                    item.reason = "Empty, recently modified, or size/modification time changed"
                    continue
                item.fingerprint = video_fingerprint(item.path)
                if snapshot(item.path) != current:
                    item.reason = "Changed while fingerprinting"
                    continue
            item.size, item.mtime_ns = current
            previous = registry.status(item.fingerprint, scope)
            item.status = "PROCESSED" if previous == "completed" else "FAILED" if previous in {"failed", "processing"} else "READY"
            if previous == "processing":
                item.reason = "Interrupted earlier job; retryable"
        except OSError as exc:
            item.reason = str(exc)
    return recordings


def select_video(requested: Path | None, source: Path, root: Path, registry: "Registry",
                 interval: float, scope: str, force: bool = False, listing: bool = False) -> Recording | None:
    if requested:
        path = resolve_path(requested, root)
        if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(f"Unsupported video type: {path.suffix}")
        recordings = [Recording(path)]
        LOG.info("Explicit VOD: %s", path)
    else:
        recordings = discover(source)
    inspect_recordings(recordings, registry, interval, scope)
    for item in recordings:
        LOG.info("%s | %s%s", item.status, item.path.name, f" | {item.reason}" if item.reason else "")
    if listing:
        return None
    for item in recordings:
        if item.status in {"READY", "FAILED"} or (force and item.status == "PROCESSED"):
            LOG.info("Recording stable: YES | Previously processed: %s", "YES" if item.status == "PROCESSED" else "NO")
            LOG.info("Selected VOD: %s", item.path)
            return item
    raise RuntimeError("No eligible finished VOD. Check the status above and retry after recording/remux finishes. "
                       "Use --force-video to revisit a completed VOD, or --resume-output for saved analysis.")


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self.entries = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.entries = data["recordings"]
            if data.get("version") != 1 or not isinstance(self.entries, dict) or any(
                not isinstance(value, dict) or value.get("status") not in {"processing", "completed", "failed"}
                for value in self.entries.values()
            ):
                raise ValueError("invalid registry structure")
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError(f"Invalid VOD registry {path}; restore it from backup before processing.") from exc

    def status(self, fingerprint: str, scope: str) -> str | None:
        return self.entries.get(f"{fingerprint}:{scope}", {}).get("status")

    @contextmanager
    def job(self, item: Recording, output: Path, scope: str):
        key = f"{item.fingerprint}:{scope}"
        entry = dict(source_filename=item.path.name, source_path=str(item.path), size_bytes=item.size,
                     modification_time_ns=item.mtime_ns, fingerprint=item.fingerprint,
                     processing_date=datetime.now(timezone.utc).isoformat(), output_directory=str(output),
                     scope=scope, status="processing")
        self.entries[key] = entry
        self.save()
        try:
            yield
        except BaseException:
            entry["status"] = "failed"
            self.save()
            raise
        else:
            entry["status"] = "completed"
            entry["processing_date"] = datetime.now(timezone.utc).isoformat()
            self.save()

    def save(self):
        atomic_write_json(self.path, {"version": 1, "recordings": self.entries})


@contextmanager
def registry_lock(output_root: Path):
    """OS lock releases on process death; serialize runs sharing a registry/cache."""
    output_root.mkdir(parents=True, exist_ok=True)
    with (output_root / ".vod_registry.lock").open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another ingestion run is using this output directory. Wait for it to finish.") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
