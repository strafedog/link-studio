import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from link_studio import control
from link_studio.constants import LINK2_PROFILE, LINK2C_PRO_PROFILE
from link_studio.ctlserver import ControlServer
from link_studio.presets import Preset
from link_studio.theme import load_current_omarchy_palette
from link_studio.watch import AutomationSettings, node_holders


def _store(presets, default=None):
    return Mock(presets=presets, default_index=default)


class _Camera:
    def __init__(self, profile=LINK2C_PRO_PROFILE, zoom=100):
        self.profile = profile
        self.values = {"zoom": zoom, "pan": 0, "tilt": 0}
        self.calls = []

    def get_control(self, key):
        return self.values[key]

    def set_control(self, key, value):
        self.calls.append(("control", key, value))
        self.values[key] = value

    def set_digital_view(self, pan, tilt, zoom=None):
        self.calls.append(("view", pan, tilt, zoom))
        self.values.update(pan=pan, tilt=tilt)
        if zoom is not None:
            self.values["zoom"] = zoom
        return pan, tilt

    def save_digital_view(self):
        self.calls.append(("save",))

    def set_video_mode(self, mode, verify_streaming=True):
        self.calls.append(("mode", mode, verify_streaming))
        return mode

    def set_feature(self, bit, enabled):
        self.calls.append(("feature", bit, enabled))

    def get_feature(self, _bit):
        return True

    def set_framing(self, framing):
        self.calls.append(("framing", framing))
        return framing


class PresetApplyTests(unittest.TestCase):
    def test_digital_ptz_preset_moves_crop_in_one_write(self):
        camera = _Camera()
        control.apply_hardware_values(camera, {"zoom": 200, "pan": 30, "tilt": -10}, True)
        self.assertIn(("view", 30, -10, 200), camera.calls)
        self.assertNotIn(("control", "pan", 30), camera.calls)

    def test_gimbal_preset_uses_uvc_controls(self):
        camera = _Camera(LINK2_PROFILE)
        control.apply_hardware_values(camera, {"zoom": 150, "pan": 30, "tilt": 5}, True)
        self.assertIn(("control", "pan", 30), camera.calls)
        self.assertFalse(any(call[0] == "view" for call in camera.calls))

    def test_ai_mode_skipped_without_stream(self):
        camera = _Camera()
        self.assertIsNone(control.apply_hardware_values(camera, {"mode": "tracking"}, False))
        self.assertFalse(any(call[0] == "mode" for call in camera.calls))
        self.assertEqual(
            control.apply_hardware_values(camera, {"mode": "tracking"}, True), "tracking"
        )

    def test_find_preset_by_number_or_name(self):
        store = _store([Preset("Desk", {}), Preset("Pete CloudHound", {})])
        self.assertEqual(control.find_preset(store, "2")[0], 1)
        self.assertEqual(control.find_preset(store, "pete cloudhound")[0], 1)
        with self.assertRaises(control.CommandError):
            control.find_preset(store, "missing")

    def test_restore_applies_default_preset(self):
        camera = _Camera()
        store = _store([Preset("Call", {"framing": "head", "mode": "tracking"})], default=0)
        message = control.restore_on_connect(camera, store)
        self.assertIn("Call", message)
        self.assertIn("tracking mode skipped", message)
        self.assertIn(("framing", "head"), camera.calls)

    def test_restore_without_default_resends_crop_when_zoomed(self):
        camera = _Camera(zoom=200)
        control.restore_on_connect(camera, _store([]))
        self.assertEqual(camera.calls, [("view", 0, 0, None)])


class CommandTests(unittest.TestCase):
    def test_relative_zoom_clamps_and_saves_crop(self):
        camera = _Camera(zoom=390)
        self.assertEqual(control.execute(camera, ["zoom", "+50"], _store([])), {"zoom": 400})
        self.assertIn(("save",), camera.calls)

    def test_move_on_digital_ptz(self):
        camera = _Camera()
        self.assertEqual(
            control.execute(camera, ["move", "left", "10"], _store([])), {"pan": -10, "tilt": 0}
        )

    def test_bad_choices_raise_command_error(self):
        camera = _Camera()
        for args in (["framing", "whole_body"], ["mode", "overhead"], ["zoom", "x"], ["nope"]):
            with self.subTest(args=args), self.assertRaises(control.CommandError):
                control.execute(camera, args, _store([]))

    def test_switch_toggle(self):
        camera = _Camera()
        self.assertEqual(control.execute(camera, ["hdr"], _store([])), {"hdr": False})
        self.assertEqual(control.execute(camera, ["mirror", "on"], _store([])), {"mirror": True})


class SocketTests(unittest.TestCase):
    def test_round_trip_through_window_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctl.sock"
            seen = []

            def handler(args, reply):
                seen.append(args)
                reply({"zoom": 150})

            server = ControlServer(handler, lambda run: run(), path=path)
            server.start()
            try:
                with patch.object(control, "socket_path", return_value=path):
                    reply = control.send_to_gui(["zoom", "150"])
            finally:
                server.stop()
            self.assertEqual(reply, {"zoom": 150, "via": "window"})
            self.assertEqual(seen, [["zoom", "150"]])
            self.assertFalse(path.exists())

    def test_stale_socket_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ctl.sock"
            import socket

            stale = socket.socket(socket.AF_UNIX)
            stale.bind(str(path))
            stale.close()
            with patch.object(control, "socket_path", return_value=path):
                self.assertIsNone(control.send_to_gui(["zoom", "100"]))
            self.assertFalse(path.exists())


class WatchTests(unittest.TestCase):
    def test_automation_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "automation.json"
            AutomationSettings("Call", None).save(path)
            self.assertEqual(AutomationSettings.load(path), AutomationSettings("Call", None))
            path.write_text("not json")
            self.assertEqual(AutomationSettings.load(path), AutomationSettings())

    def test_node_holders_skips_link_studio_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, cmdline in ((101, b"firefox\0"), (102, b"python\0-m\0link_studio\0")):
                (proc / str(pid) / "fd").mkdir(parents=True)
                (proc / str(pid) / "cmdline").write_bytes(cmdline)
                os.symlink("/dev/video0", proc / str(pid) / "fd" / "7")
            (proc / "103" / "fd").mkdir(parents=True)
            os.symlink("/dev/video2", proc / "103" / "fd" / "3")
            self.assertEqual(node_holders("/dev/video0", proc=proc), {101})
            self.assertEqual(
                node_holders("/dev/video0", exclude_link_studio=False, proc=proc), {101, 102}
            )


class ThemeTests(unittest.TestCase):
    def test_user_colors_file_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "link-studio").mkdir()
            (Path(directory) / "link-studio/colors.toml").write_text(
                'mode = "dark"\naccent = "#ff8800"\n'
            )
            env = {"XDG_CONFIG_HOME": directory, "XDG_STATE_HOME": directory}
            with patch.dict(os.environ, env):
                palette = load_current_omarchy_palette()
            self.assertEqual(palette.accent, "#ff8800")


if __name__ == "__main__":
    unittest.main()
