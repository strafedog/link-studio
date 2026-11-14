"""Unix socket a running window listens on for `link-studio ctl` commands."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from .control import socket_path

LOGGER = logging.getLogger("link_studio.ctlserver")

Reply = Callable[[dict[str, Any]], None]
# Called on the GTK main thread with the command and a reply callback (call it once).
Handler = Callable[[list[str], Reply], None]


class ControlServer:
    def __init__(
        self,
        handler: Handler,
        schedule: Callable[[Callable[[], None]], None],
        path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.handler = handler
        self.schedule = schedule  # runs a callable on the main thread (GLib.idle_add)
        self.path = path or socket_path()
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with suppress(FileNotFoundError):
            self.path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        server.listen(4)
        self._socket = server
        self._thread = threading.Thread(target=self._serve, name="link-ctl", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, self._socket = self._socket, None
        if server:
            with suppress(OSError):
                server.shutdown(socket.SHUT_RDWR)
            server.close()
            with suppress(FileNotFoundError):
                self.path.unlink()

    def _serve(self) -> None:
        while self._socket is not None:
            try:
                client, _ = self._socket.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(client,), daemon=True).start()

    def _client(self, client: socket.socket) -> None:
        with client:
            try:
                client.settimeout(5)
                data = b""
                while not data.endswith(b"\n") and len(data) < 65536:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                args = json.loads(data)["args"]
                if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                    raise TypeError("args must be a list of strings")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._send(client, {"error": f"bad request: {exc}"})
                return

            done = threading.Event()
            result: dict[str, Any] = {}

            def reply(payload: dict[str, Any]) -> None:
                result.update(payload)
                done.set()

            def run() -> None:
                try:
                    self.handler(args, reply)
                except Exception as exc:  # report, never kill the main loop
                    reply({"error": str(exc)})

            self.schedule(run)
            if not done.wait(self.timeout):
                result = {"error": "timed out waiting for the window"}
            self._send(client, {**result, "via": "window"})

    @staticmethod
    def _send(client: socket.socket, payload: dict[str, Any]) -> None:
        with suppress(OSError):
            client.settimeout(5)
            client.sendall(json.dumps(payload).encode() + b"\n")
