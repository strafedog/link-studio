import math
import unittest

from link_studio.effects import EffectProcessor
from link_studio.matting import RvmMatter, plan_shapes


class PlanShapesTests(unittest.TestCase):
    def check(self, width, height):
        internal_w, internal_h, ratio, matting_h = plan_shapes(width, height)
        self.assertEqual(internal_w % 16, 0)
        self.assertEqual(internal_h % 16, 0)
        # OpenVINO/ONNX Resize computes floor(size * ratio): it must land exactly.
        self.assertEqual(math.floor(width * ratio), internal_w)
        self.assertEqual(math.floor(matting_h * ratio), internal_h)
        return internal_w, internal_h, matting_h

    def test_common_16_9_sizes_need_no_resize(self):
        for width, height in ((1920, 1080), (1280, 720), (3840, 2160)):
            internal_w, internal_h, matting_h = self.check(width, height)
            self.assertEqual((internal_w, internal_h, matting_h), (512, 288, height))

    def test_odd_aspect_ratios_get_a_matting_height(self):
        for width, height in ((640, 480), (1080, 1920), (1000, 777), (320, 240)):
            self.check(width, height)


class FallbackTests(unittest.TestCase):
    def test_missing_model_reports_reason(self):
        matter = RvmMatter(model=None)
        matter._model_path = None
        self.assertIsNotNone(matter.unavailable_reason)
        self.assertIsNone(matter.alpha(__import__("numpy").zeros((72, 128, 3), "uint8")))

    def test_matting_setting_is_validated(self):
        processor = EffectProcessor()
        self.assertEqual(processor.settings.matting, "rvm")
        processor.update(matting="mediapipe")
        with self.assertRaises(ValueError):
            processor.update(matting="nonsense")


if __name__ == "__main__":
    unittest.main()
