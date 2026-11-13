"""Small custom-drawn widgets: digital crop minimap and microphone pickup diagrams."""

from __future__ import annotations

import math
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

ACCENT = (0.18, 0.78, 0.62)


def _foreground(widget: Gtk.Widget) -> tuple[float, float, float]:
    color = widget.get_color()
    return color.red, color.green, color.blue


def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class CropMinimap(Gtk.DrawingArea):
    """The full sensor frame with the current digital crop drawn as a draggable box.

    ``get_view`` returns (zoom percent, centre x, centre y) with the centre in 0..1
    frame coordinates; ``on_move`` receives a new centre while the box is dragged.
    """

    def __init__(
        self,
        get_view: Callable[[], tuple[int, float, float]],
        on_move: Callable[[float, float], None],
    ):
        super().__init__(content_width=224, content_height=126)
        self._get_view = get_view
        self._on_move = on_move
        self._drag_origin: tuple[float, float] | None = None
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_args: setattr(self, "_drag_origin", None))
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda _g, _n, x, y: self._move_to(x, y))
        self.add_controller(click)
        self.set_cursor_from_name("grab")

    def _frame(self) -> tuple[float, float, float, float]:
        width, height = self.get_width(), self.get_height()
        frame_w = min(width, height * 16 / 9)
        frame_h = frame_w * 9 / 16
        return (width - frame_w) / 2, (height - frame_h) / 2, frame_w, frame_h

    def _box(self) -> tuple[float, float, float, float]:
        zoom, cx, cy = self._get_view()
        scale = 100 / max(100, zoom)
        half = scale / 2
        cx = min(max(cx, half), 1 - half)
        cy = min(max(cy, half), 1 - half)
        return cx, cy, scale, scale

    def _move_to(self, x: float, y: float) -> None:
        fx, fy, fw, fh = self._frame()
        self._on_move(min(max((x - fx) / fw, 0.0), 1.0), min(max((y - fy) / fh, 0.0), 1.0))
        self.queue_draw()

    def _drag_begin(self, _gesture: Gtk.GestureDrag, x: float, y: float) -> None:
        self._drag_origin = (x, y)

    def _drag_update(self, _gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_origin:
            self._move_to(self._drag_origin[0] + dx, self._drag_origin[1] + dy)

    def _draw(self, _area, cr, _width: int, _height: int) -> None:
        cr.new_path()
        fg = _foreground(self)
        fx, fy, fw, fh = self._frame()
        _rounded_rect(cr, fx, fy, fw, fh, 6)
        cr.set_source_rgba(*fg, 0.08)
        cr.fill_preserve()
        cr.set_source_rgba(*fg, 0.25)
        cr.set_line_width(1)
        cr.stroke()

        cx, cy, sw, sh = self._box()
        bw, bh = fw * sw, fh * sh
        bx, by = fx + fw * cx - bw / 2, fy + fh * cy - bh / 2
        inset = 2
        _rounded_rect(cr, bx + inset, by + inset, bw - 2 * inset, bh - 2 * inset, 4)
        cr.set_source_rgba(*ACCENT, 0.12)
        cr.fill_preserve()
        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(2)
        cr.stroke()

        # Location pin at the crop centre.
        px, py = fx + fw * cx, fy + fh * cy
        cr.set_source_rgb(*ACCENT)
        cr.arc(px, py - 5, 5, 0, 2 * math.pi)
        cr.fill()
        cr.move_to(px - 4, py - 3)
        cr.line_to(px, py + 4)
        cr.line_to(px + 4, py - 3)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(*fg, 0.9)
        cr.arc(px, py - 5, 1.6, 0, 2 * math.pi)
        cr.fill()


# Polar pickup shapes, r(theta) in 0..1 with theta = 0 pointing at the speaker
# (towards the bottom of the diagram, in front of the camera).
def _standard(theta: float) -> float:
    return 0.82 + 0.1 * math.cos(theta)


def _wide(theta: float) -> float:
    return max(0.0, 0.5 + 0.5 * math.cos(theta)) ** 0.8 * 0.95


def _focus(theta: float) -> float:
    return max(0.0, math.cos(theta)) ** 2.2 * 0.62


def _original(_theta: float) -> float:
    return 0.95


MIC_PATTERNS: dict[str, Callable[[float], float]] = {
    "standard": _standard,
    "wide": _wide,
    "focus": _focus,
    "original": _original,
}


class MicPatternIcon(Gtk.DrawingArea):
    """Top-down sketch of a microphone mode's pickup area around the camera."""

    def __init__(self, mode: str, size: int = 64):
        super().__init__(content_width=size, content_height=size, valign=Gtk.Align.CENTER)
        self._mode = mode
        self.set_draw_func(self._draw)

    def _draw(self, _area, cr, width: int, height: int) -> None:
        cr.new_path()
        fg = _foreground(self)
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2 - 2

        # Reference rings.
        cr.set_line_width(1)
        for ring in (1.0, 0.66, 0.33):
            cr.set_source_rgba(*fg, 0.10 if ring < 1 else 0.18)
            cr.new_sub_path()
            cr.arc(cx, cy, radius * ring, 0, 2 * math.pi)
            cr.stroke()

        pattern = MIC_PATTERNS.get(self._mode, _standard)
        steps = 96
        for index in range(steps + 1):
            theta = 2 * math.pi * index / steps
            r = radius * pattern(theta)
            # theta 0 = down (in front of the camera, where the speaker sits).
            x, y = cx + r * math.sin(theta), cy + r * math.cos(theta)
            if index == 0:
                cr.move_to(x, y)
            else:
                cr.line_to(x, y)
        cr.close_path()
        filled = 0.55 if self._mode != "original" else 0.30
        cr.set_source_rgba(*ACCENT, filled)
        cr.fill_preserve()
        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(1.5)
        if self._mode == "original":
            cr.set_dash([3, 2])
        cr.stroke()
        cr.set_dash([])

        # The camera as a small pill, lens facing the speaker.
        cr.set_source_rgba(*fg, 0.95)
        _rounded_rect(cr, cx - 8, cy - 4, 16, 8, 4)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.8)
        cr.arc(cx - 3.5, cy, 2, 0, 2 * math.pi)
        cr.fill()

        # Speaker dot in front of the camera.
        cr.set_source_rgba(*fg, 0.9)
        cr.arc(cx, cy + radius * 0.48, 2.5, 0, 2 * math.pi)
        cr.fill()
