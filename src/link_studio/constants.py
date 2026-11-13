from __future__ import annotations

from dataclasses import dataclass

APP_ID = "io.github.linkstudio.LinkStudio"
APP_NAME = "Link Studio"
INSTA360_VENDOR_ID = 0x2E1A

SUPPORTED_PRODUCTS = {
    0x4C01: "Insta360 Link",
    0x4C02: "Insta360 Link 2C",
    0x4C03: "Insta360 Link 2 Pro",
    0x4C04: "Insta360 Link 2",
    0x4C05: "Insta360 Link 2C Pro",
}


@dataclass(frozen=True, slots=True)
class ControlSpec:
    key: str
    label: str
    control_id: int
    minimum: int
    maximum: int
    step: int = 1
    default: int = 0
    unit: str = ""


# Standard UVC controls exposed by the Link 2 through V4L2.
STANDARD_CONTROLS = {
    "brightness": ControlSpec("brightness", "Brightness", 0x00980900, 0, 100, 1, 50),
    "contrast": ControlSpec("contrast", "Contrast", 0x00980901, 0, 100, 1, 50),
    "saturation": ControlSpec("saturation", "Saturation", 0x00980902, 0, 100, 1, 50),
    "hue": ControlSpec("hue", "Hue", 0x00980903, -15, 15, 1, 0),
    "white_balance_auto": ControlSpec(
        "white_balance_auto", "Automatic white balance", 0x0098090C, 0, 1, 1, 1
    ),
    "anti_flicker": ControlSpec("anti_flicker", "Anti-flicker", 0x00980918, 0, 3, 1, 3),
    "white_balance_temperature": ControlSpec(
        "white_balance_temperature", "Color temperature", 0x0098091A, 2000, 10000, 100, 6400, "K"
    ),
    "sharpness": ControlSpec("sharpness", "Sharpness", 0x0098091B, 0, 100, 1, 50),
    "pan": ControlSpec("pan", "Pan", 0x009A0908, -145, 145, 1, 0, "°"),
    "tilt": ControlSpec("tilt", "Tilt", 0x009A0909, -90, 100, 1, 0, "°"),
    "focus": ControlSpec("focus", "Focus", 0x009A090A, 0, 100, 1, 50),
    "focus_auto": ControlSpec("focus_auto", "Autofocus", 0x009A090C, 0, 1, 1, 1),
    "zoom": ControlSpec("zoom", "Zoom", 0x009A090D, 100, 400, 1, 100, "%"),
}

VIDEO_MODES = {
    "normal": (0x00, 0x00, "Normal"),
    "tracking": (0x01, 0x00, "AI Tracking"),
    "whiteboard": (0x04, 0x01, "Whiteboard"),
    "overhead": (0x05, 0x03, "Overhead"),
    "deskview": (0x06, 0x10, "DeskView"),
}

FRAMING_MODES = {"head": 1, "half_body": 2, "whole_body": 3}
ANTI_FLICKER_LABELS = ("Disabled", "50 Hz", "60 Hz", "Auto")
TRACKING_SPEED_MAX = 255

# Link 2/2C microphone DSP modes carried by XU9 selector 0x07. The default
# firmware value is Voice Focus. Music Balance is the unprocessed/no-denoise
# mode exposed by the official client.
AUDIO_MODES = {"music_balance": 0, "voice_focus": 1, "voice_suppression": 2}


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Per-model differences in the vendor protocol and the UI options they allow."""

    key: str
    video_modes: dict[str, tuple[int, int, str]]
    framing_modes: dict[str, int]
    framing_labels: tuple[str, ...]
    audio_modes: dict[str, int]
    audio_labels: tuple[str, ...]
    audio_unit: int = 9
    audio_selector: int = 0x07
    # 2C Pro: pan/tilt are a digital crop window (XU10 selector 0x13), not a gimbal.
    digital_ptz: bool = False
    # 2C Pro: feature-mask bit 0 is "Smart composition"; framing only applies with it on.
    smart_composition_bit: int | None = None
    # 2C Pro: Link Controller fills bytes 38-47 of the 61-byte mode payload on enable.
    mode_payload_extra: bool = False
    # 2C Pro: per-gesture bits on XU9 selector 0x05; feature bit 4 is the master switch.
    gesture_bits: dict[str, int] | None = None
    # 2C Pro: DeskView vertical correction lives in bytes 52-53 of the mode payload.
    deskview_correction: bool = False
    # Standard controls the firmware exposes but that measurably do nothing.
    hidden_controls: frozenset[str] = frozenset()
    # 2C Pro: manual ISO/shutter via XU9 0x19/0x1D give a black picture; auto only.
    manual_exposure: bool = True
    # One-line explanations shown under each audio mode.
    audio_descriptions: dict[str, str] | None = None


LINK2_PROFILE = ModelProfile(
    key="link2",
    video_modes=VIDEO_MODES,
    framing_modes=FRAMING_MODES,
    framing_labels=("Head", "Half body", "Whole body"),
    audio_modes={"voice_focus": 1, "voice_suppression": 2, "music_balance": 0},
    audio_labels=("Voice Focus", "Voice Suppression", "Music Balance"),
)

# Insta360 Link 2C Pro (2e1a:4c05), decoded from Link Controller USB captures on
# firmware v0.3.5.8_build3 (2026-11-13). No gimbal and no Overhead mode; "AI Tracking"
# is Auto Framing (mode 0x07). Smart Whiteboard (0x0A detect, 0x08 + corner quad) is
# not wired up: Link Controller finds the board in the video itself.
LINK2C_PRO_PROFILE = ModelProfile(
    key="link2c_pro",
    video_modes={
        "normal": (0x00, 0x00, "Normal"),
        "tracking": (0x07, 0x00, "Auto Framing"),
        "whiteboard": (0x04, 0x00, "Whiteboard"),
        "deskview": (0x06, 0x00, "DeskView"),
    },
    framing_modes={"off": 0, "head": 1, "half_body": 2},
    framing_labels=("Off", "Head", "Half body"),
    audio_modes={"standard": 0, "wide": 1, "focus": 2, "original": 3},
    audio_labels=("Standard", "Wide", "Focus", "Original"),
    audio_unit=10,
    audio_selector=0x1F,
    digital_ptz=True,
    smart_composition_bit=0,
    mode_payload_extra=True,
    gesture_bits={"gesture_framing": 0x02, "gesture_zoom": 0x04, "gesture_whiteboard": 0x08},
    deskview_correction=True,
    hidden_controls=frozenset({"hue"}),
    manual_exposure=False,
    audio_descriptions={
        "standard": "Everyday use. Picks up the room, lifts voices and trims some "
        "background noise.",
        "wide": "Meetings with several people in front of the camera. Keeps voices in "
        "front, cuts noise and voices from behind.",
        "focus": "Noisy places. Narrow pickup about 1 m in front; suppresses other "
        "voices and background noise.",
        "original": "No noise reduction. Records the room as it sounds, for the most "
        "natural audio.",
    },
)

MODEL_PROFILES = {0x4C05: LINK2C_PRO_PROFILE}


def profile_for(product_id: int) -> ModelProfile:
    return MODEL_PROFILES.get(product_id, LINK2_PROFILE)
