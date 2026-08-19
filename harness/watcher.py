"""Workspace observation: event-driven, never polled.

Uses watchdog's native observers (inotify / FSEvents /
ReadDirectoryChanges) so the engine sleeps until the OS reports a
change. Saves are debounced per file: a burst of writes becomes one
snapshot after a quiet interval, and a snapshot is emitted only when
the content hash actually changed.

The same observer also watches the session's spool directory, which is
how runs executed in a separate terminal (via tools/irun) reach the
engine: the wrapper drops cmd/out/err/status files and touches `done`,
and the `done` creation event triggers intake here.

A pack may have no workspace at all, in which case none of this is
started and the session runs purely on transcript and clock events.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import threading
from typing import Any, Callable, Dict, List, Optional

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    HAVE_WATCHDOG = True
except Exception:  # noqa: BLE001 - any import trouble means "not available"
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    HAVE_WATCHDOG = False


class WatchUnavailable(RuntimeError):
    pass


Emit = Callable[[Dict[str, Any]], None]


def _is_ignored(rel: str, globs: List[str]) -> bool:
    parts = rel.replace(os.sep, "/").split("/")
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return True
    name = parts[-1]
    return any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(rel, g) for g in globs)


class _SaveHandler(FileSystemEventHandler):
    def __init__(self, root: str, debounce_s: float, max_bytes: int, globs: List[str], emit: Emit) -> None:
        self.root = os.path.abspath(root)
        self.debounce_s = debounce_s
        self.max_bytes = max_bytes
        self.globs = globs
        self.emit = emit
        self._lock = threading.Lock()
        self._timers: Dict[str, threading.Timer] = {}
        self._hashes: Dict[str, str] = {}

    # watchdog callbacks -------------------------------------------------
    def on_created(self, event: Any) -> None:
        if not event.is_directory:
            self._touch(event.src_path)

    def on_modified(self, event: Any) -> None:
        if not event.is_directory:
            self._touch(event.src_path)

    def on_moved(self, event: Any) -> None:
        if not event.is_directory:
            self._touch(event.dest_path)

    # debounce -----------------------------------------------------------
    def _touch(self, path: str) -> None:
        rel = os.path.relpath(os.path.abspath(path), self.root)
        if rel.startswith("..") or _is_ignored(rel, self.globs):
            return
        with self._lock:
            timer = self._timers.pop(rel, None)
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(self.debounce_s, self._snapshot, [rel])
            timer.daemon = True
            self._timers[rel] = timer
            timer.start()

    def _snapshot(self, rel: str, collect: bool = False) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._timers.pop(rel, None)
        path = os.path.join(self.root, rel)
        try:
            with open(path, "rb") as fh:
                raw = fh.read(self.max_bytes + 1)
        except OSError:
            return None
        if b"\x00" in raw[:4096]:
            return None  # binary; the interviewer can only read text
        truncated = len(raw) > self.max_bytes
        raw = raw[: self.max_bytes]
        digest = hashlib.sha256(raw).hexdigest()
        if self._hashes.get(rel) == digest:
            return None
        self._hashes[rel] = digest
        text = str(raw, "utf-8", "replace")
        row = {
            "kind": "file_saved",
            "path": rel.replace(os.sep, "/"),
            "content": text,
            "sha256": digest,
            "bytes": len(raw),
            "truncated": truncated,
        }
        if collect:
            return row
        self.emit(row)
        return None

    def flush_pending(self) -> List[Dict[str, Any]]:
        """Cancel pending debounce timers and snapshot their files NOW,
        returning the rows instead of emitting them — so a caller can
        place them at an exact point in the transcript (task
        boundaries, session end)."""
        with self._lock:
            pending = list(self._timers)
            for rel in pending:
                self._timers.pop(rel).cancel()
        rows = []
        for rel in pending:
            row = self._snapshot(rel, collect=True)
            if row is not None:
                rows.append(row)
        return rows


class _SpoolHandler(FileSystemEventHandler):
    def __init__(self, spool: str, emit: Emit) -> None:
        self.spool = os.path.abspath(spool)
        self.emit = emit
        self._seen: set = set()
        self._lock = threading.Lock()

    def on_created(self, event: Any) -> None:
        if event.is_directory or os.path.basename(event.src_path) != "done":
            return
        entry = os.path.dirname(os.path.abspath(event.src_path))
        with self._lock:
            if entry in self._seen:
                return
            self._seen.add(entry)
        row = _read_spool_entry(entry)
        if row is not None:
            self.emit(row)


def _read_spool_entry(entry: str) -> Optional[Dict[str, Any]]:
    def read(name: str, default: str = "") -> str:
        try:
            with open(os.path.join(entry, name), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return default

    cmd = read("cmd.txt").strip()
    if not cmd:
        return None
    try:
        status = int(read("status.txt", "-1").strip() or "-1")
    except ValueError:
        status = -1
    try:
        duration_ms = int(read("ms.txt", "-1").strip() or "-1")
    except ValueError:
        duration_ms = -1
    return {
        "kind": "run_executed",
        "cmd": cmd,
        "out": read("out.txt"),
        "err": read("err.txt"),
        "exit_status": status,
        "duration_ms": duration_ms,
        "source": "external",
    }


class WorkspaceWatch:
    """Owns the observer threads for one session."""

    def __init__(
        self,
        workspace: str,
        spool: Optional[str],
        *,
        debounce_ms: int,
        snapshot_max_bytes: int,
        ignore_globs: List[str],
        emit: Emit,
    ) -> None:
        if not HAVE_WATCHDOG:
            raise WatchUnavailable(
                "the 'watchdog' package is required to observe a workspace; "
                "install requirements or run a pack with no workspace"
            )
        self._observer = Observer()
        self._observer.daemon = True
        self._save_handler = _SaveHandler(
            workspace, debounce_ms / 1000.0, snapshot_max_bytes, ignore_globs, emit
        )
        self._observer.schedule(self._save_handler, workspace, recursive=True)
        if spool:
            self._observer.schedule(_SpoolHandler(spool, emit), spool, recursive=True)

    def flush_pending(self) -> List[Dict[str, Any]]:
        return self._save_handler.flush_pending()

    def register_content(self, rel: str, raw: bytes) -> None:
        """Pre-register content the ENGINE is about to write, so the
        resulting change is not re-emitted as a candidate save. Hash
        exactly what a snapshot would hash: the truncated prefix."""
        digest = hashlib.sha256(raw[: self._save_handler.max_bytes]).hexdigest()
        with self._save_handler._lock:
            self._save_handler._hashes[rel.replace(os.sep, "/")] = digest

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        try:
            self._observer.stop()
            self._observer.join(timeout=3)
        except Exception:
            pass
