import struct
import threading
import unittest

from link_studio.camera import VIDIOC_QUERYCTRL, Camera, _V4L2QueryControl
from link_studio.constants import LINK2_PROFILE, LINK2C_PRO_PROFILE, profile_for


def _fake_2c_pro(feature_mask: int = 0x01C4, framing: int = 1, audio: int = 0):
    camera = object.__new__(Camera)
    camera.profile = LINK2C_PRO_PROFILE
    camera._digital_pan = camera._digital_tilt = 0
    camera._lock = threading.RLock()
    state = {
        (9, 0x1B): feature_mask.to_bytes(2, "little"),
        (9, 0x13): bytes([framing]),
        (10, 0x1F): bytes([audio]),
    }
    writes = []

    def xu_set(unit, selector, payload):
        writes.append((unit, selector, bytes(payload)))
        state[(unit, selector)] = bytes(payload)

    camera.xu_set = xu_set
    camera.xu_get = lambda unit, selector, *_args: state.get((unit, selector), bytes(61))
    camera.xu_length = lambda *_args, **_kwargs: 61
    camera.get_control = lambda key, _orig=Camera.get_control, _c=camera: (
        200 if key == "zoom" else _orig(_c, key)
    )
    return camera, writes, state


class Link2CProProfileTests(unittest.TestCase):
    def test_profile_selected_by_product_id(self):
        self.assertIs(profile_for(0x4C05), LINK2C_PRO_PROFILE)
        self.assertIs(profile_for(0x4C04), LINK2_PROFILE)
        self.assertEqual(set(LINK2C_PRO_PROFILE.video_modes), {"normal", "tracking"})
        self.assertEqual(LINK2C_PRO_PROFILE.video_modes["tracking"][0], 0x07)

    def test_queryctrl_layout(self):
        self.assertEqual(struct.calcsize("<II32siiiiI2I"), 68)
        self.assertEqual(VIDIOC_QUERYCTRL, 0xC0445624)
        self.assertEqual(len(bytes(_V4L2QueryControl())), 68)

    def test_audio_modes_use_xu10_selector_0x1f(self):
        camera, writes, _state = _fake_2c_pro()
        self.assertEqual(camera.set_audio_mode("focus"), "focus")
        self.assertEqual(writes[-1], (10, 0x1F, b"\x02"))
        self.assertTrue(camera.get_noise_cancellation())
        camera.set_audio_mode("original")
        self.assertFalse(camera.get_noise_cancellation())

    def test_framing_toggles_smart_composition_bit(self):
        camera, writes, state = _fake_2c_pro(feature_mask=0x01C4)
        self.assertEqual(camera.get_framing(), "off")
        self.assertEqual(camera.set_framing("half_body"), "half_body")
        self.assertIn((9, 0x13, b"\x02"), writes)
        self.assertEqual(int.from_bytes(state[(9, 0x1B)], "little") & 1, 1)
        self.assertEqual(camera.set_framing("off"), "off")
        self.assertEqual(int.from_bytes(state[(9, 0x1B)], "little") & 1, 0)

    def test_digital_pan_tilt_payload(self):
        camera, writes, _state = _fake_2c_pro()
        self.assertEqual(camera.set_control("pan", -145), -145)
        unit, selector, payload = writes[-1]
        self.assertEqual((unit, selector), (10, 0x13))
        zoom, x, y, a, b = struct.unpack("<HHHBB", payload)
        self.assertEqual((zoom, a, b), (200, 0x14, 0x14))
        self.assertLess(x, 0x0100)
        self.assertEqual(y, 0x7D09)
        camera.set_control("tilt", 100)
        _zoom, _x, y, _a, _b = struct.unpack("<HHHBB", writes[-1][2])
        self.assertEqual(y, 0)
        self.assertEqual(camera.get_control("pan"), -145)

    def test_auto_framing_payload_matches_link_controller(self):
        camera, writes, _state = _fake_2c_pro()
        camera._write_video_mode_buffer(0x07, 0)
        payload = writes[-1][2]
        self.assertEqual(payload[0], 0x07)
        self.assertEqual(payload[38:42], b"\xff\xff\xff\xff")
        self.assertEqual(payload[42:44], b"\x1a\x0e")
        self.assertEqual(payload[46:48], b"\x1a\x0e")


if __name__ == "__main__":
    unittest.main()
