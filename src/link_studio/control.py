"""Scriptable camera control: `link-studio ctl ...`.

Commands go to a running Link Studio window over a Unix socket when there is one, so its
widgets stay in step, and are otherwise run directly against the camera. The camera
backend is cooperative (it never detaches uvcvideo), so a direct run is safe while a
browser or the preview owns the video stream.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from .camera import FEATURE_GESTURE_ZOOM, FEATURE_HDR, FEATURE_MIRROR
from .constants import LINK2_PROFILE, ModelProfile
from .presets import Preset, PresetStore

MOVE_STEP = 5
ZOOM_RANGE = (100, 400)
SWITCHES = {"hdr": FEATURE_HDR, "mirror": FEATURE_MIRROR}

USAGE = """usage: link-studio ctl <command> [argument]

commands:
  state                       print the camera state as JSON
  presets                     list scene presets (* = default)
  preset <name|number>        apply a scene preset
  restore                     apply the default preset (as on connect)
  mode <name>                 normal, tracking, whiteboard, deskview, ...
  framing <name>              e.g. head, half_body (model dependent)
  audio <name>                microphone mode (model dependent)
  zoom <100-400|+N|-N>        zoom percent, absolute or relative
  move <up|down|left|right> [degrees]
  center                      reset pan, tilt and zoom
  hdr|mirror <on|off|toggle>
"""


class CommandError(ValueError):
    """A ctl command that cannot run (bad arguments, unknown preset, ...)."""


def _profile(camera: object) -> ModelProfile:
    return getattr(camera, "profile", LINK2_PROFILE)


def runtime_directory() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(base) if base else Path.home() / ".cache") / "link-studio"


def socket_path() -> Path:
    return runtime_directory() / "ctl.sock"


# ------------------------------------------------------------------ presets


def apply_hardware_values(camera: Any, values: dict[str, Any], streaming: bool) -> str | None:
    """Write a scene preset's camera settings. Returns the mode set, or None if skipped.

    AI modes other than normal are only accepted while the camera streams, so without a
    stream the mode is left alone rather than written into the void.
    """

    profile = _profile(camera)
    for key in ("brightness", "contrast", "saturation", "hue", "sharpness", "anti_flicker"):
        if key in values:
            camera.set_control(key, int(values[key]))
    if profile.digital_ptz and {"pan", "tilt"} <= values.keys():
        # The 2C Pro ignores UVC pan/tilt; its crop window moves through one vendor write.
        camera.set_digital_view(
            int(values["pan"]), int(values["tilt"]), int(values.get("zoom", 100))
        )
    else:
        for key in ("zoom", "pan", "tilt"):
            if key in values:
                camera.set_control(key, int(values[key]))
    if "focus_auto" in values:
        camera.set_control("focus_auto", bool(values["focus_auto"]))
    if not values.get("focus_auto", True) and "focus" in values:
        camera.set_control("focus", int(values["focus"]))
    if "white_balance_auto" in values:
        camera.set_control("white_balance_auto", bool(values["white_balance_auto"]))
    if not values.get("white_balance_auto", True) and "white_balance_temperature" in values:
        camera.set_control("white_balance_temperature", int(values["white_balance_temperature"]))
    if "auto_exposure" in values:
        camera.set_auto_exposure(bool(values["auto_exposure"]))
    if "exposure_compensation" in values:
        camera.set_exposure_compensation(int(values["exposure_compensation"]))
    if not values.get("auto_exposure", True):
        if "iso" in values:
            camera.set_manual_iso(int(values["iso"]))
        if "shutter_us" in values:
            camera.set_shutter(int(values["shutter_us"]))
    for key, bit in (
        ("hdr", FEATURE_HDR),
        ("mirror", FEATURE_MIRROR),
        ("gesture_zoom", FEATURE_GESTURE_ZOOM),
    ):
        if key in values:
            camera.set_feature(bit, bool(values[key]))
    if "framing" in values and values["framing"] in profile.framing_modes:
        camera.set_framing(str(values["framing"]))
    if "tracking_speed" in values:
        camera.set_tracking_speed(int(values["tracking_speed"]))
    if "audio_mode" in values:
        camera.set_audio_mode(str(values["audio_mode"]))
    elif "noise_cancellation" in values:
        camera.set_noise_cancellation(bool(values["noise_cancellation"]))
    mode = str(values.get("mode", "normal"))
    if mode not in profile.video_modes or (mode != "normal" and not streaming):
        return None
    camera.set_video_mode(mode, verify_streaming=streaming)
    return mode


def find_preset(store: PresetStore, ref: str) -> tuple[int, Preset]:
    """Look a preset up by 1-based number or by name (case-insensitive)."""

    presets = store.presets
    if ref.isdigit() and 1 <= int(ref) <= len(presets):
        return int(ref) - 1, presets[int(ref) - 1]
    for index, preset in enumerate(presets):
        if preset.name.casefold() == ref.strip().casefold():
            return index, preset
    raise CommandError(f"no preset called {ref!r}")


def restore_on_connect(camera: Any, store: PresetStore, streaming: bool = False) -> str:
    """What happens when the camera (re)appears: default preset, else the remembered crop."""

    index = store.default_index
    if index is not None and 0 <= index < len(store.presets):
        preset = store.presets[index]
        mode = apply_hardware_values(camera, preset.values, streaming)
        wanted = preset.values.get("mode", "normal")
        note = "" if mode or wanted == "normal" else f" ({wanted} mode skipped: not streaming)"
        return f"applied default preset {preset.name!r}{note}"
    if _profile(camera).digital_ptz and camera.get_control("zoom") > ZOOM_RANGE[0]:
        camera.set_digital_view(camera.get_control("pan"), camera.get_control("tilt"))
        return "re-sent the remembered crop"
    return "nothing to restore (no default preset)"


# ----------------------------------------------------------------- commands


def _choice(name: str, value: str | None, options: dict[str, Any] | tuple[str, ...]) -> str:
    if value not in options:
        raise CommandError(f"{name} must be one of: {', '.join(options)}")
    return value


def _switch(current: bool, value: str | None) -> bool:
    if value in {"on", "1", "true"}:
        return True
    if value in {"off", "0", "false"}:
        return False
    if value in {"toggle", None}:
        return not current
    raise CommandError("expected on, off or toggle")


def _target_zoom(current: int, value: str | None) -> int:
    if not value:
        raise CommandError("zoom needs a value")
    try:
        number = int(value)
    except ValueError as exc:
        raise CommandError(f"bad zoom value {value!r}") from exc
    target = current + number if value[0] in "+-" else number
    return max(ZOOM_RANGE[0], min(ZOOM_RANGE[1], target))


def execute(
    camera: Any, args: list[str], store: PresetStore | None = None, streaming: bool = True
) -> dict[str, Any]:
    """Run one hardware command directly. Returns a JSON-able result."""

    if not args:
        raise CommandError("no command given")
    command, value = args[0], (args[1] if len(args) > 1 else None)
    profile = _profile(camera)
    store = store if store is not None else PresetStore()

    if command == "state":
        state = camera.read_state()
        state.pop("device_information", None)
        return {"state": state}
    if command == "presets":
        return {
            "presets": [
                {"number": i + 1, "name": p.name, "default": i == store.default_index}
                for i, p in enumerate(store.presets)
            ]
        }
    if command == "preset":
        _index, preset = find_preset(store, value or "")
        mode = apply_hardware_values(camera, preset.values, streaming)
        return {"applied": preset.name, "mode": mode}
    if command == "restore":
        return {"restored": restore_on_connect(camera, store, streaming)}
    if command == "mode":
        mode = _choice("mode", value, profile.video_modes)
        return {"mode": camera.set_video_mode(mode, verify_streaming=streaming)}
    if command == "framing":
        return {"framing": camera.set_framing(_choice("framing", value, profile.framing_modes))}
    if command == "audio":
        return {"audio_mode": camera.set_audio_mode(_choice("audio", value, profile.audio_modes))}
    if command == "zoom":
        zoom = _target_zoom(camera.get_control("zoom"), value)
        if profile.digital_ptz:
            camera.set_digital_view(camera.get_control("pan"), camera.get_control("tilt"), zoom)
            camera.save_digital_view()
        else:
            camera.set_control("zoom", zoom)
        return {"zoom": zoom}
    if command == "move":
        direction = _choice("move", value, ("up", "down", "left", "right"))
        try:
            step = int(args[2]) if len(args) > 2 else MOVE_STEP
        except ValueError as exc:
            raise CommandError("move step must be a whole number of degrees") from exc
        dx, dy = {"up": (0, step), "down": (0, -step), "left": (-step, 0), "right": (step, 0)}[
            direction
        ]
        pan, tilt = camera.get_control("pan") + dx, camera.get_control("tilt") + dy
        if profile.digital_ptz:
            pan, tilt = camera.set_digital_view(pan, tilt)
            camera.save_digital_view()
        else:
            camera.set_control("pan", pan)
            camera.set_control("tilt", tilt)
        return {"pan": pan, "tilt": tilt}
    if command == "center":
        camera.center()
        return {"centered": True}
    if command in SWITCHES:
        bit = SWITCHES[command]
        enabled = _switch(camera.get_feature(bit), value)
        camera.set_feature(bit, enabled)
        return {command: enabled}
    raise CommandError(f"unknown command {command!r}")


# ------------------------------------------------------------------- socket

READ_ONLY = {"state", "presets"}


def send_to_gui(args: list[str], timeout: float = 15.0) -> dict[str, Any] | None:
    """Hand a command to a running window. None when no window is listening."""

    path = socket_path()
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(json.dumps({"args": args}).encode() + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = client.recv(65536)
                if not chunk:
                    break
                reply += chunk
    except (ConnectionRefusedError, FileNotFoundError):
        # Stale socket left by a crashed window.
        with suppress(OSError):
            path.unlink()
        return None
    return json.loads(reply or b"{}")


def run_direct(args: list[str], device_path: str | None = None) -> dict[str, Any]:
    from .camera import Camera, discover_cameras

    devices = discover_cameras()
    selected = next((d for d in devices if d.path == device_path), None)
    if selected is None and devices:
        selected = devices[0]
    if selected is None:
        raise CommandError("no Insta360 Link camera found")
    with Camera(selected) as camera:
        streaming = camera_streaming(selected.path)
        return execute(camera, args, streaming=streaming)


def dispatch(args: list[str], device_path: str | None = None) -> dict[str, Any]:
    if args and args[0] not in READ_ONLY:
        reply = send_to_gui(args)
        if reply is not None:
            if "error" in reply:
                raise CommandError(reply["error"])
            return reply
    return run_direct(args, device_path)


def camera_streaming(device_path: str) -> bool:
    """True when another process has the capture node open (a browser, OBS, the window)."""

    from .watch import node_holders

    return bool(node_holders(device_path, exclude_self=True, exclude_link_studio=False))


def main(argv: list[str]) -> int:
    device = None
    if len(argv) >= 2 and argv[0] == "--device":
        device, argv = argv[1], argv[2:]
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE, end="")
        return 0 if argv else 2
    try:
        result = dispatch(argv, device)
    except CommandError as exc:
        print(f"link-studio ctl: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"link-studio ctl: camera error: {exc}", file=sys.stderr)
        return 1
    if argv[0] == "presets":
        for item in result["presets"]:
            marker = "*" if item["default"] else " "
            print(f"{marker} {item['number']:2d}  {item['name']}")
    else:
        print(json.dumps(result, indent=2 if argv[0] == "state" else None, sort_keys=True))
    return 0
