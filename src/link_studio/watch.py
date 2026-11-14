"""`link-studio daemon`: re-apply settings on (re)connect and switch presets around calls.

Runs as a systemd user service. It watches /dev for the camera appearing (hotplug, dock,
boot) and the camera's capture node, plus any v4l2loopback node, for other programs opening
them. A non-Link-Studio process holding one of those nodes counts as a call.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import select
import struct
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger("link_studio.watch")

IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_CLOSE_NOWRITE = 0x00000010
IN_OPEN = 0x00000020
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_IGNORED = 0x00008000
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
_EVENT = struct.Struct("iIII")

CALL_START_DELAY = 3.0  # held this long before it counts (browsers probe devices briefly)
CALL_END_DELAY = 8.0  # Meet closes and reopens the camera when it changes resolution
HOTPLUG_SETTLE = 2.5  # udev applies the uaccess ACL a moment after the node appears
FALLBACK_POLL = 30.0
LINK_STUDIO_MARKERS = (b"link_studio", b"link-studio")


def default_automation_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "link-studio/automation.json"


@dataclass
class AutomationSettings:
    """Preset names to apply when a call starts or ends (None = leave the camera alone)."""

    call_start: str | None = None
    call_end: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> AutomationSettings:
        try:
            raw = json.loads((path or default_automation_path()).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()

        def name(key: str) -> str | None:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) and value.strip() else None

        return cls(call_start=name("call_start"), call_end=name("call_end"))

    def save(self, path: Path | None = None) -> None:
        path = path or default_automation_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"call_start": self.call_start, "call_end": self.call_end}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def node_holders(
    device_path: str,
    exclude_self: bool = True,
    exclude_link_studio: bool = True,
    proc: Path = Path("/proc"),
) -> set[int]:
    """PIDs (of processes we may inspect, i.e. our own) with `device_path` open."""

    holders: set[int] = set()
    me = os.getpid()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if exclude_self and pid == me:
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) != device_path:
                    continue
            except OSError:
                continue
            if exclude_link_studio:
                with suppress(OSError):
                    cmdline = (entry / "cmdline").read_bytes()
                    if any(marker in cmdline for marker in LINK_STUDIO_MARKERS):
                        break
            holders.add(pid)
            break
    return holders


class Inotify:
    def __init__(self) -> None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._add = libc.inotify_add_watch
        self._add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        self._rm = libc.inotify_rm_watch
        self._rm.argtypes = (ctypes.c_int, ctypes.c_int)
        self.fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self.watches: dict[int, str] = {}

    def add(self, path: str, mask: int) -> int:
        wd = self._add(self.fd, path.encode(), mask)
        if wd < 0:
            raise OSError(ctypes.get_errno(), f"inotify_add_watch {path} failed")
        self.watches[wd] = path
        return wd

    def remove(self, wd: int) -> None:
        self._rm(self.fd, wd)
        self.watches.pop(wd, None)

    def read(self) -> list[tuple[str, int, str]]:
        """Return (watched path, mask, name) for every pending event."""

        events = []
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return events
        offset = 0
        while offset + _EVENT.size <= len(data):
            wd, mask, _cookie, length = _EVENT.unpack_from(data, offset)
            raw = data[offset + _EVENT.size : offset + _EVENT.size + length]
            offset += _EVENT.size + length
            path = self.watches.get(wd, "")
            if mask & IN_IGNORED:
                self.watches.pop(wd, None)
            events.append((path, mask, raw.rstrip(b"\0").decode(errors="replace")))
        return events


class Daemon:
    def __init__(self) -> None:
        self.inotify = Inotify()
        self.inotify.add("/dev", IN_CREATE | IN_DELETE | IN_ATTRIB)
        self.cameras: set[str] = set()
        self.node_watches: dict[str, int] = {}
        self.in_call = False
        self._pending_since: float | None = None
        self._evaluate_at: float | None = None
        self._rescan_at: float | None = time.monotonic()  # first scan restores at startup

    # ----------------------------------------------------------- devices

    def _rescan(self) -> None:
        from .camera import discover_cameras
        from .preview import discover_virtual_camera_devices

        current = {device.path for device in discover_cameras()}
        for path in sorted(current - self.cameras):
            LOGGER.info("camera at %s", path)
            self._restore(path)
        self.cameras = current
        wanted = current | set(discover_virtual_camera_devices())
        for path in set(self.node_watches) - wanted:
            with suppress(OSError):
                self.inotify.remove(self.node_watches.pop(path))
        for path in wanted - set(self.node_watches):
            try:
                self.node_watches[path] = self.inotify.add(
                    path, IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE
                )
            except OSError as exc:
                LOGGER.warning("cannot watch %s: %s", path, exc)
        self._evaluate_at = time.monotonic()

    def _restore(self, path: str) -> None:
        from .control import run_direct

        # Always direct: a window open across an unplug holds a dead file descriptor.
        try:
            LOGGER.info("restore: %s", run_direct(["restore"], path)["restored"])
        except Exception as exc:  # keep the daemon alive whatever the camera does
            LOGGER.warning("restore on %s failed: %s", path, exc)

    # ------------------------------------------------------------- calls

    def _camera_in_use(self) -> bool:
        return any(node_holders(path) for path in self.node_watches)

    def _evaluate(self, now: float) -> None:
        in_use = self._camera_in_use()
        if in_use == self.in_call:
            self._pending_since = None
            return
        if self._pending_since is None:
            self._pending_since = now
        delay = CALL_START_DELAY if in_use else CALL_END_DELAY
        if now - self._pending_since < delay:
            self._evaluate_at = self._pending_since + delay
            return
        self._pending_since = None
        self.in_call = in_use
        LOGGER.info("call %s", "started" if in_use else "ended")
        settings = AutomationSettings.load()
        preset = settings.call_start if in_use else settings.call_end
        if preset:
            self._apply_preset(preset)

    def _apply_preset(self, name: str) -> None:
        from .control import dispatch

        try:
            LOGGER.info("applied preset %r: %s", name, dispatch(["preset", name]))
        except Exception as exc:
            LOGGER.warning("preset %r failed: %s", name, exc)

    # -------------------------------------------------------------- loop

    def run(self) -> None:
        LOGGER.info("watching for the camera and calls")
        while True:
            now = time.monotonic()
            deadlines = [d for d in (self._evaluate_at, self._rescan_at) if d is not None]
            timeout = max(0.0, min(deadlines) - now) if deadlines else FALLBACK_POLL
            readable, _, _ = select.select([self.inotify.fd], [], [], min(timeout, FALLBACK_POLL))
            now = time.monotonic()
            if readable:
                for path, mask, name in self.inotify.read():
                    if path == "/dev":
                        if name.startswith("video") and mask & (IN_CREATE | IN_DELETE | IN_ATTRIB):
                            self._rescan_at = now + HOTPLUG_SETTLE
                    elif mask & (IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE):
                        # Let the opener finish (and our own reads settle) before scanning /proc.
                        self._evaluate_at = min(self._evaluate_at or now + 0.5, now + 0.5)
            if self._rescan_at is not None and now >= self._rescan_at:
                self._rescan_at = None
                self._rescan()
            if self._evaluate_at is None or now >= self._evaluate_at:
                self._evaluate_at = None
                self._evaluate(now)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        Daemon().run()
    except KeyboardInterrupt:
        return 0
    return 0
