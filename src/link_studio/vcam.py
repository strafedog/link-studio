"""Headless virtual camera for `link-studio daemon`.

With v4l2loopback's exclusive_caps=1 (needed for Chrome-based browsers to list the device)
the loopback node only advertises itself as a camera while something is writing to it. So
the daemon keeps a producer open all the time, sending a cheap placeholder (the effect's
background image, or a dark frame) at a couple of frames per second. When another program
opens the node, the physical camera starts with the software effects of the call preset
(or the default preset) and processed frames replace the placeholder. Once nothing has
held the node for a few seconds, the camera is released again.

While a Link Studio window is open the daemon stays off the loopback node, so the window's
own Virtual camera switch keeps working as before. A call that is already live keeps going.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import suppress
from typing import Any

LOGGER = logging.getLogger("link_studio.vcam")

WIDTH, HEIGHT, FPS = 1280, 720, 30
PLACEHOLDER_INTERVAL = 0.5
STOP_DELAY = 6.0  # Meet closes and reopens the device when it renegotiates
HOLDER_CHECK_INTERVAL = 2.0  # a /proc scan costs ~50 ms, so not every tick
PLACEHOLDER_COLOR = (24, 26, 32)


def gui_running() -> bool:
    """True when a Link Studio window answers on its control socket."""

    from .control import socket_path

    path = socket_path()
    if not path.exists():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(0.5)
        try:
            client.connect(str(path))
        except OSError:
            return False
    return True


def call_effects() -> dict[str, Any]:
    """Software effect settings from the call preset, else the default preset."""

    from .presets import PresetStore
    from .watch import AutomationSettings

    try:
        store = PresetStore()
        store.load()
    except Exception as exc:
        LOGGER.warning("cannot read presets: %s", exc)
        return {}
    preset = None
    name = AutomationSettings.load().call_start
    if name:
        preset = next((p for p in store.presets if p.name == name), None)
    if preset is None and store.default_index is not None:
        with suppress(IndexError):
            preset = store.presets[store.default_index]
    if preset is None and store.presets:
        preset = store.presets[0]
    effects = preset.values.get("software_effects") if preset else None
    if not isinstance(effects, dict):
        return {}
    restored = dict(effects)
    if isinstance(restored.get("tracking_area"), list):
        restored["tracking_area"] = tuple(restored["tracking_area"])
    if isinstance(restored.get("pause_areas"), list):
        restored["pause_areas"] = tuple(tuple(region) for region in restored["pause_areas"])
    # Portrait orientations would change the frame size the loopback was opened with.
    restored["orientation"] = "identity"
    return restored


class VirtualCameraService:
    def __init__(self, loopback: str) -> None:
        self.loopback = loopback
        self._lock = threading.RLock()
        self._publisher: Any = None
        self._stream: Any = None
        self._camera_path: str | None = None
        self._last_holder = 0.0
        self._last_holder_check = 0.0
        self._placeholder: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._main_loop: Any = None

    # ---------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread:
            return
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import GLib, Gst

        Gst.init(None)
        # GStreamer bus watches (camera errors) dispatch on the default GLib context.
        self._main_loop = GLib.MainLoop()
        threading.Thread(target=self._main_loop.run, daemon=True, name="vcam-glib").start()
        self._thread = threading.Thread(target=self._run, daemon=True, name="vcam")
        self._thread.start()
        LOGGER.info("virtual camera service on %s", self.loopback)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._stop_camera()
            self._close_publisher()
        if self._main_loop:
            self._main_loop.quit()

    # ------------------------------------------------------------ control

    def update(self, consumers: bool, camera_path: str | None) -> None:
        """Called by the daemon after open/close events on the loopback node."""

        with self._lock:
            self._camera_path = camera_path
            if consumers:
                self._last_holder = time.monotonic()
                if self._stream is None and camera_path:
                    self._start_camera(camera_path)

    @property
    def live(self) -> bool:
        return self._stream is not None

    # ----------------------------------------------------------- internals

    def _ensure_publisher(self) -> bool:
        if self._publisher is not None:
            return True
        from .preview import VirtualCameraPublisher

        publisher = VirtualCameraPublisher(self.loopback, WIDTH, HEIGHT, FPS)
        try:
            publisher.start()
        except Exception as exc:
            LOGGER.warning("cannot open %s: %s", self.loopback, exc)
            return False
        self._publisher = publisher
        LOGGER.info("publishing placeholder on %s", self.loopback)
        return True

    def _close_publisher(self) -> None:
        publisher, self._publisher = self._publisher, None
        if publisher:
            publisher.stop()
            LOGGER.info("released %s", self.loopback)

    def _placeholder_frame(self) -> Any:
        if self._placeholder is not None:
            return self._placeholder
        from .effects import EffectProcessor, EffectSettings
        from .preview import Frame

        effects = call_effects()
        image_path = effects.get("background_image")
        data = None
        try:
            import numpy as np

            processor = EffectProcessor()
            canvas = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
            if image_path:
                settings = EffectSettings(background_image=image_path)
                processor._load_runtime()
                data = processor._background(canvas, settings).tobytes()
            if data is None:
                canvas[:] = PLACEHOLDER_COLOR
                data = canvas.tobytes()
        except Exception as exc:
            LOGGER.warning("placeholder image failed: %s", exc)
            data = bytes(PLACEHOLDER_COLOR) * (WIDTH * HEIGHT)
        self._placeholder = Frame(WIDTH, HEIGHT, WIDTH * 3, data, 0)
        return self._placeholder

    def _start_camera(self, camera_path: str) -> None:
        from .preview import PreviewConfig, PreviewStream

        stream = PreviewStream(camera_path, PreviewConfig(WIDTH, HEIGHT, FPS))
        effects = call_effects()
        try:
            if effects:
                stream.set_effects(**effects)
        except Exception as exc:
            LOGGER.warning("ignoring stored effects: %s", exc)
        stream.add_consumer(self._forward)
        try:
            stream.start()
        except Exception as exc:
            LOGGER.warning("camera %s did not start: %s", camera_path, exc)
            stream.close()
            return
        self._stream = stream
        self._placeholder = None  # pick up a changed background next time
        LOGGER.info(
            "live: %s -> %s (effect %s)", camera_path, self.loopback, effects.get("mode", "none")
        )

    def _stop_camera(self) -> None:
        stream, self._stream = self._stream, None
        if stream:
            stream.remove_consumer(self._forward)
            stream.close()
            LOGGER.info("camera released; placeholder on %s", self.loopback)

    def _forward(self, frame: Any) -> None:
        publisher = self._publisher
        if publisher is not None:
            publisher.push(frame)

    def _run(self) -> None:
        while not self._stop.wait(PLACEHOLDER_INTERVAL):
            try:
                self._tick()
            except Exception:
                LOGGER.exception("virtual camera tick failed")

    def _tick(self) -> None:
        with self._lock:
            stream = self._stream
            if stream is not None:
                error = stream.take_error()
                if error:
                    LOGGER.warning("camera stream: %s", error)
                now = time.monotonic()
                if now - self._last_holder_check >= HOLDER_CHECK_INTERVAL:
                    from .watch import node_holders

                    self._last_holder_check = now
                    if node_holders(self.loopback):
                        self._last_holder = now
                idle = now - self._last_holder > STOP_DELAY
                if not stream.running or idle:
                    self._stop_camera()
                return
            if gui_running():
                self._close_publisher()
                return
            if self._ensure_publisher():
                self._publisher.push(self._placeholder_frame())
