"""Robust Video Matting (RVM) person matting on an Intel GPU through OpenVINO.

MediaPipe's selfie segmenter only sees a 256x256 picture, so its mask is blobby once it is
scaled to 1080p. RVM (MobileNetV3 variant, https://github.com/PeterL1n/RobustVideoMatting,
GPL-3.0 weights, not bundled in this repository) predicts a real alpha matte at the full
frame resolution and carries recurrent state between frames, so hair and beard edges stay
sharp and stable. On an Arc iGPU it takes about 14 ms per 1080p frame; on the CPU it would
take three to four cores, so by default it only runs on a GPU and callers fall back to
MediaPipe otherwise.

The model is loaded as OpenVINO IR (rvm_mobilenetv3.xml/.bin). Shapes are fixed per frame
size because the GPU plugin is much faster with static shapes; compiling takes a couple of
seconds the first time (cached afterwards), so it happens on a background thread and
``alpha()`` returns None until the network is ready.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

MODEL_NAME = "rvm_mobilenetv3.xml"
# Width of the low-resolution pass inside RVM. 512 keeps the base network cheap; RVM's
# deep guided filter then refines the matte at the full frame resolution.
INTERNAL_WIDTH = 512


def model_candidates() -> list[Path]:
    paths = []
    override = os.environ.get("LINK_STUDIO_RVM_MODEL")
    if override:
        paths.append(Path(override))
    paths.append(Path(__file__).resolve().parent / "models" / MODEL_NAME)
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    paths.append(data_home / "link-studio" / "models" / MODEL_NAME)
    return paths


def find_model() -> Path | None:
    for path in model_candidates():
        if path.is_file() and path.with_suffix(".bin").is_file():
            return path
    return None


def plan_shapes(width: int, height: int) -> tuple[int, int, float, int]:
    """Return (internal_w, internal_h, downsample_ratio, matting_height).

    RVM's encoder needs the internal size to be a multiple of 16 and OpenVINO computes it as
    floor(size * ratio), so the ratio gets a tiny epsilon. If the frame's aspect ratio does
    not land on a multiple of 16, the frame is resized to ``matting_height`` before matting.
    """

    scale = min(1.0, INTERNAL_WIDTH / width)
    internal_w = max(16, round(width * scale / 16) * 16)
    ratio = (internal_w + 0.01) / width
    internal_h = max(16, math.floor(height * ratio) // 16 * 16)
    matting_height = height
    if math.floor(height * ratio) != internal_h:
        matting_height = round(internal_h / (internal_w / width))
        for candidate in (matting_height, matting_height + 1, matting_height - 1):
            if math.floor(candidate * ratio) == internal_h:
                matting_height = candidate
                break
    return internal_w, internal_h, ratio, matting_height


class RvmMatter:
    """Stateful RVM alpha matting for one video stream."""

    def __init__(self, model: Path | None = None, device: str | None = None) -> None:
        self._model_path = model or find_model()
        self._device_request = device or os.environ.get("LINK_STUDIO_RVM_DEVICE", "GPU")
        self._lock = threading.Lock()
        self._core: Any = None
        self._ov: Any = None
        self._compiled: Any = None
        self._requests: list[Any] = []
        self._shape: tuple[int, int] | None = None
        self._plan: tuple[int, int, float, int] | None = None
        self._compiling: tuple[int, int] | None = None
        self._turn = 0
        self._failed: str | None = None
        self.device: str | None = None

    @property
    def unavailable_reason(self) -> str | None:
        if self._failed:
            return self._failed
        if self._model_path is None:
            return "RVM model not installed"
        return None

    def _load_core(self) -> Any:
        if self._core is not None:
            return self._core
        import openvino as ov

        core = ov.Core()
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        cache_dir = cache / "link-studio" / "openvino"
        cache_dir.mkdir(parents=True, exist_ok=True)
        core.set_property({"CACHE_DIR": str(cache_dir)})
        devices = core.available_devices
        wanted = self._device_request.upper()
        if wanted not in devices and not any(d.startswith(wanted + ".") for d in devices):
            raise RuntimeError(f"OpenVINO device {wanted} not available (have {devices})")
        self._ov, self._core, self.device = ov, core, wanted
        return core

    def _compile(self, width: int, height: int) -> None:
        try:
            core = self._load_core()
            ov = self._ov
            from openvino.preprocess import PrePostProcessor

            internal_w, internal_h, ratio, matting_h = plan_shapes(width, height)
            model = core.read_model(str(self._model_path))
            states = self._state_shapes(internal_w, internal_h)
            model.reshape(
                {
                    "src": [1, 3, matting_h, width],
                    **{f"r{i + 1}i": shape for i, shape in enumerate(states)},
                    "downsample_ratio": [1],
                }
            )
            ppp = PrePostProcessor(model)
            ppp.input("src").tensor().set_element_type(ov.Type.u8).set_layout(ov.Layout("NHWC"))
            ppp.input("src").preprocess().convert_element_type(ov.Type.f32).scale(255.0)
            ppp.input("src").model().set_layout(ov.Layout("NCHW"))
            model = ppp.build()
            config: dict[str, Any] = {"PERFORMANCE_HINT": "LATENCY"}
            if self.device == "GPU":
                import openvino.properties.intel_gpu.hint as gpu_hint

                # LOW throttle lets the host thread sleep instead of spinning on the GPU
                # (measured: same 14 ms latency, half the CPU time).
                config[gpu_hint.queue_throttle] = gpu_hint.ThrottleLevel.LOW
            compiled = core.compile_model(model, self.device, config)
            requests = [compiled.create_infer_request() for _ in range(2)]
            ratio_tensor = ov.Tensor(self._np().array([ratio], dtype=self._np().float32))
            for request in requests:
                request.set_tensor("downsample_ratio", ratio_tensor)
            with self._lock:
                self._compiled = compiled
                self._requests = requests
                self._shape = (width, height)
                self._plan = (internal_w, internal_h, ratio, matting_h)
                self._compiling = None
                self._reset_states_locked()
            LOG.info(
                "RVM ready on %s for %dx%d (internal %dx%d)",
                self.device,
                width,
                height,
                internal_w,
                internal_h,
            )
        except Exception as exc:
            LOG.warning("RVM matting unavailable: %s", exc)
            with self._lock:
                self._failed = str(exc)
                self._compiling = None

    @staticmethod
    def _np() -> Any:
        import numpy

        return numpy

    @staticmethod
    def _state_shapes(internal_w: int, internal_h: int) -> list[list[int]]:
        return [
            [1, 16, internal_h // 2, internal_w // 2],
            [1, 20, internal_h // 4, internal_w // 4],
            [1, 40, internal_h // 8, internal_w // 8],
            [1, 64, internal_h // 16, internal_w // 16],
        ]

    def _reset_states_locked(self) -> None:
        if not self._requests or self._plan is None:
            return
        np = self._np()
        for request in self._requests:
            for index, shape in enumerate(self._state_shapes(*self._plan[:2])):
                request.set_tensor(f"r{index + 1}i", self._ov.Tensor(np.zeros(shape, np.float32)))
        self._turn = 0

    def reset(self) -> None:
        """Forget temporal state (scene cut, stream restart)."""

        with self._lock:
            self._reset_states_locked()

    def alpha(self, image: Any) -> Any | None:
        """Return a float32 HxW alpha matte for an RGB uint8 frame, or None if not ready."""

        if self.unavailable_reason:
            return None
        height, width = image.shape[:2]
        with self._lock:
            ready = self._shape == (width, height) and self._requests
            if not ready:
                if self._compiling != (width, height):
                    self._compiling = (width, height)
                    self._compiled = None
                    self._requests = []
                    self._shape = None
                    threading.Thread(
                        target=self._compile,
                        args=(width, height),
                        daemon=True,
                        name="link-studio-rvm-compile",
                    ).start()
                return None
            request = self._requests[self._turn % 2]
            following = self._requests[(self._turn + 1) % 2]
            matting_h = self._plan[3] if self._plan else height
            self._turn += 1
        np = self._np()
        frame = image
        if matting_h != height:
            import cv2

            frame = cv2.resize(image, (width, matting_h), interpolation=cv2.INTER_AREA)
        request.set_tensor(
            "src", self._ov.Tensor(np.ascontiguousarray(frame[None]), shared_memory=True)
        )
        request.infer()
        alpha = request.get_tensor("pha").data[0, 0]
        # Ping-pong: this request's recurrent outputs become the next request's inputs
        # without copying them through numpy.
        for index in range(1, 5):
            following.set_tensor(f"r{index}i", request.get_tensor(f"r{index}o"))
        if matting_h != height:
            import cv2

            return cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
        return alpha.copy()

    def close(self) -> None:
        with self._lock:
            self._requests = []
            self._compiled = None


RVM_ONNX_URL = (
    "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/"
    "rvm_mobilenetv3_fp32.onnx"
)
RVM_ONNX_SHA256 = "88d4531297118f595bf2fd60f6f566aec2e559393802d1f436c380f0cbbd2828"


def convert(onnx_path: Path, output_dir: Path) -> Path:
    """Convert the RVM ONNX release to FP16-compressed OpenVINO IR in output_dir."""

    import hashlib

    import openvino as ov

    digest = hashlib.sha256(onnx_path.read_bytes()).hexdigest()
    if digest != RVM_ONNX_SHA256:
        raise SystemExit(f"{onnx_path}: sha256 {digest} does not match {RVM_ONNX_SHA256}")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / MODEL_NAME
    ov.save_model(ov.Core().read_model(str(onnx_path)), str(target), compress_to_fp16=True)
    return target


def _main(argv: list[str]) -> int:
    """python -m link_studio.matting convert <rvm_mobilenetv3_fp32.onnx> [output dir]"""

    if len(argv) not in (2, 3) or argv[0] != "convert":
        print(_main.__doc__)
        print(f"Model: {RVM_ONNX_URL}")
        return 2
    output = Path(argv[2]) if len(argv) == 3 else model_candidates()[-1].parent
    print(convert(Path(argv[1]), output))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
