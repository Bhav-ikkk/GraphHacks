"""Lightweight CLI progress so long Falkor / ingest steps are not silent."""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from typing import Iterator


class Progress:
    """Print step lines to stderr as work happens."""

    def __init__(self, *, enabled: bool = True, stream=None) -> None:
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._t0 = time.monotonic()
        self._last = self._t0

    def step(self, message: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        elapsed = now - self._t0
        delta = now - self._last
        self._last = now
        # ASCII-only so Windows consoles (cp1252) do not mangle the loader.
        line = f"  .. {message}  (+{delta:.1f}s, {elapsed:.1f}s total)\n"
        self.stream.write(line)
        self.stream.flush()

    def done(self, message: str = "done") -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self._t0
        line = f"  ok {message}  ({elapsed:.1f}s)\n"
        self.stream.write(line)
        self.stream.flush()


_active: Progress | None = None


def get_progress() -> Progress | None:
    return _active


def set_progress(progress: Progress | None) -> Progress | None:
    global _active
    previous = _active
    _active = progress
    return previous


def step(message: str) -> None:
    if _active is not None:
        _active.step(message)


@contextmanager
def track(title: str | None = None, *, enabled: bool = True) -> Iterator[Progress]:
    progress = Progress(enabled=enabled)
    previous = set_progress(progress)
    if title and enabled:
        try:
            sys.stderr.write(f"\n> {title}\n")
            sys.stderr.flush()
        except UnicodeEncodeError:
            sys.stderr.write(f"\n> {title.encode('ascii', 'replace').decode('ascii')}\n")
            sys.stderr.flush()
    try:
        yield progress
    finally:
        set_progress(previous)
