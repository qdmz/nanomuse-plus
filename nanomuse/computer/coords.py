"""The one place where a point in the picture becomes a point on the screen.

The model answers in pixels of the picture it was shown. The hands move in the pixels of
their own screen space: X11 root pixels on Linux, points on macOS, physical pixels on
Windows (pyautogui and libnut are DPI-aware there). The two are not the same size — the
picture is scaled down for the model, a Retina capture is twice its points — and every
"the click is off by a bit" in the hands came from mixing them. So:

* :func:`picture_size` decides how big the picture is: the screen space capped at
  ``max_width``, then UI-TARS's ``smart_resize`` (factor 28, the Qwen-VL family's own
  preprocessing) so the model's internal resize is the identity and its absolute pixels
  *are* picture pixels.
* :class:`Mapping` carries the two sizes and maps picture pixels → screen pixels (for the
  hands) and → fractions of the screen (for the overlay's marker). Coordinates can also
  come in on the 0–1000 grid (``coords = "norm1000"``) or as a box whose centre is meant.

Portions derived from UI-TARS-desktop (https://github.com/bytedance/UI-TARS-desktop),
© 2025 Bytedance, Inc. and its affiliates, Apache-2.0: ``smart_resize`` is
``smartResizeForV15`` (packages/ui-tars/action-parser), ``from_norm`` and ``box_centre``
follow ``parseBoxToScreenCoords`` (packages/ui-tars/sdk).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

IMAGE_FACTOR = 28
MIN_PIXELS = 100 * IMAGE_FACTOR * IMAGE_FACTOR
MAX_PIXELS = 16384 * IMAGE_FACTOR * IMAGE_FACTOR
MAX_RATIO = 200
NORM = 1000  # the 0–1000 grid UI-TARS and Qwen3-VL answer in by default
# The most pixels a picture for the model has (the shared budget rule: ≤ 2 Mpx before
# encoding). A 4K screen comes down to half its side, 1080p (2.07 Mpx) is trimmed a
# little, 1680×1050 and below pass untouched. A request with a few 4K JPEGs in it was what
# hit the relay's body cap (413).
PICTURE_MAX_PIXELS = 2_000_000


def _round_by(num: float, factor: int) -> int:
    return int(round(num / factor)) * factor


def _floor_by(num: float, factor: int) -> int:
    return int(math.floor(num / factor)) * factor


def _ceil_by(num: float, factor: int) -> int:
    return int(math.ceil(num / factor)) * factor


def smart_resize(
    height: int,
    width: int,
    factor: int = IMAGE_FACTOR,
    min_pixels: int = MIN_PIXELS,
    max_pixels: int = MAX_PIXELS,
    max_ratio: int = MAX_RATIO,
) -> tuple[int, int]:
    """The size Qwen-VL resizes a ``width``×``height`` picture to: both sides multiples of
    ``factor``, the pixel count between ``min_pixels`` and ``max_pixels``, the aspect ratio
    kept as closely as the rounding allows. Returns ``(width, height)``. A picture already
    of this size goes through the model unchanged, so its pixel coordinates are ours."""
    if height <= 0 or width <= 0:
        raise ValueError("a picture needs a positive width and height")
    if max(height, width) / min(height, width) > max_ratio:
        raise ValueError(
            f"the aspect ratio must be below {max_ratio}, got "
            f"{max(height, width) / min(height, width):.1f}"
        )
    h_bar = max(factor, _round_by(height, factor))
    w_bar = max(factor, _round_by(width, factor))
    if h_bar * w_bar > max_pixels:
        beta = math.sqrt((height * width) / max_pixels)
        h_bar = _floor_by(height / beta, factor)
        w_bar = _floor_by(width / beta, factor)
    elif h_bar * w_bar < min_pixels:
        beta = math.sqrt(min_pixels / (height * width))
        h_bar = _ceil_by(height * beta, factor)
        w_bar = _ceil_by(width * beta, factor)
    return w_bar, h_bar


def picture_size(
    screen_w: int, screen_h: int, max_width: int = 0, max_pixels: int = PICTURE_MAX_PIXELS
) -> tuple[int, int]:
    """How big the picture handed to the model is for a ``screen_w``×``screen_h`` screen:
    capped at ``max_width`` (0 = no cap) and at ``max_pixels`` (0 = no cap) keeping the
    aspect ratio, then :func:`smart_resize`. ``(0, 0)`` when the screen's size is unknown."""
    if screen_w <= 0 or screen_h <= 0:
        return 0, 0
    w, h = float(screen_w), float(screen_h)
    if max_width and w > max_width:
        h = h * max_width / w
        w = float(max_width)
    if max_pixels and w * h > max_pixels:
        k = math.sqrt(max_pixels / (w * h))
        w, h = w * k, h * k
    try:
        return smart_resize(max(1, int(round(h))), max(1, int(round(w))))
    except ValueError:
        return int(round(w)), int(round(h))


def box_centre(box: Sequence[float]) -> tuple[float, float]:
    """The point a box means: its centre. Two numbers are a point already."""
    values = [float(v) for v in box]
    if len(values) == 2:
        return values[0], values[1]
    if len(values) != 4:
        raise ValueError("a box is [x1, y1, x2, y2] (or a point [x, y])")
    x1, y1, x2, y2 = values
    return (x1 + x2) / 2, (y1 + y2) / 2


def from_norm(x: float, y: float, image_w: int, image_h: int) -> tuple[float, float]:
    """A point on the 0–1000 grid → pixels of an ``image_w``×``image_h`` picture, rounded to
    the grid's resolution the way UI-TARS does (so 500 on a 1000-wide picture is 500.0)."""
    px = round(x / NORM * image_w * NORM) / NORM
    py = round(y / NORM * image_h * NORM) / NORM
    return px, py


@dataclass(frozen=True)
class Mapping:
    """Picture pixels ↔ the hands' screen pixels for one screenshot."""

    image_w: int
    image_h: int
    screen_w: int
    screen_h: int

    @property
    def scale(self) -> tuple[float, float]:
        """``(sx, sy)``: screen pixels per picture pixel; 1 when a size is unknown."""
        sx = self.screen_w / self.image_w if self.image_w and self.screen_w else 1.0
        sy = self.screen_h / self.image_h if self.image_h and self.screen_h else 1.0
        return sx, sy

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        """Picture pixels → screen pixels, clamped to the picture first (a model that
        answers one pixel past the edge means the edge)."""
        sx, sy = self.scale
        if self.image_w:
            x = min(max(x, 0.0), float(self.image_w))
        if self.image_h:
            y = min(max(y, 0.0), float(self.image_h))
        return round(x * sx, 2), round(y * sy, 2)

    def fractions(self, x: float, y: float) -> tuple[float, float]:
        """Picture pixels → fractions of the screen (0–1), for an overlay that is sized to
        the display rather than to either pixel space."""
        if not (self.image_w and self.image_h):
            return 0.0, 0.0
        fx = min(max(x / self.image_w, 0.0), 1.0)
        fy = min(max(y / self.image_h, 0.0), 1.0)
        return round(fx, 4), round(fy, 4)


__all__ = [
    "IMAGE_FACTOR",
    "MAX_PIXELS",
    "MIN_PIXELS",
    "NORM",
    "PICTURE_MAX_PIXELS",
    "Mapping",
    "box_centre",
    "from_norm",
    "picture_size",
    "smart_resize",
]
