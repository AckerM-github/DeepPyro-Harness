"""Pixel wordmark for the startup splash.

The glyphs are pre-rendered FIGlet "ansi_shadow" output, so the package carries
no font dependency. Rendering only colors the existing block characters with a
per-character gradient and pads every line to the same width, which keeps the
wordmark horizontally centered inside the splash body.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text

ColorStop = tuple[float, str]

DEEPPYRO_ART: tuple[str, ...] = (
    "██████╗ ███████╗███████╗██████╗ ██████╗ ██╗   ██╗██████╗  ██████╗ ",
    "██╔══██╗██╔════╝██╔════╝██╔══██╗██╔══██╗╚██╗ ██╔╝██╔══██╗██╔═══██╗",
    "██║  ██║█████╗  █████╗  ██████╔╝██████╔╝ ╚████╔╝ ██████╔╝██║   ██║",
    "██║  ██║██╔══╝  ██╔══╝  ██╔═══╝ ██╔═══╝   ╚██╔╝  ██╔══██╗██║   ██║",
    "██████╔╝███████╗███████╗██║     ██║        ██║   ██║  ██║╚██████╔╝",
    "╚═════╝ ╚══════╝╚══════╝╚═╝     ╚═╝        ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ",
)

HARNESS_ART: tuple[str, ...] = (
    "██╗  ██╗ █████╗ ██████╗ ███╗   ██╗███████╗███████╗███████╗",
    "██║  ██║██╔══██╗██╔══██╗████╗  ██║██╔════╝██╔════╝██╔════╝",
    "███████║███████║██████╔╝██╔██╗ ██║█████╗  ███████╗███████╗",
    "██╔══██║██╔══██║██╔══██╗██║╚██╗██║██╔══╝  ╚════██║╚════██║",
    "██║  ██║██║  ██║██║  ██║██║ ╚████║███████╗███████║███████║",
    "╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚══════╝╚══════╝",
)

FIRE_STOPS: tuple[ColorStop, ...] = (
    (0.0, "#ff4d6d"),
    (0.55, "#ff9e64"),
    (1.0, "#ffd75f"),
)
ICE_STOPS: tuple[ColorStop, ...] = (
    (0.0, "#7dcfff"),
    (0.55, "#7aa2f7"),
    (1.0, "#bb9af7"),
)

COMPACT_WORD = "DEEPPYRO HARNESS"

#: Width of the padded wordmark block (the widest pre-rendered row).
ART_WIDTH = max(len(line) for line in (*DEEPPYRO_ART, *HARNESS_ART))


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    raw = value.lstrip("#")
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)


def _mix(start: str, end: str, t: float) -> str:
    start_r, start_g, start_b = _hex_to_rgb(start)
    end_r, end_g, end_b = _hex_to_rgb(end)
    ratio = max(0.0, min(1.0, t))
    red = round(start_r + (end_r - start_r) * ratio)
    green = round(start_g + (end_g - start_g) * ratio)
    blue = round(start_b + (end_b - start_b) * ratio)
    return f"#{red:02x}{green:02x}{blue:02x}"


def color_at(stops: Sequence[ColorStop], t: float) -> str:
    """Interpolate a hex color from a stop list at position t in [0, 1]."""
    position = max(0.0, min(1.0, t))
    if position <= stops[0][0]:
        return stops[0][1]
    for index in range(1, len(stops)):
        left_t, left_color = stops[index - 1]
        right_t, right_color = stops[index]
        if position <= right_t:
            span = right_t - left_t
            local = 0.0 if span <= 0 else (position - left_t) / span
            return _mix(left_color, right_color, local)
    return stops[-1][1]


def gradient_line(line: str, stops: Sequence[ColorStop]) -> Text:
    """Color each character of one line along the gradient stops."""
    text = Text()
    last = max(len(line) - 1, 1)
    for index, char in enumerate(line):
        text.append(char, style="bold " + color_at(stops, index / last))
    return text


def _padded_line(line: str, stops: Sequence[ColorStop], width: int) -> Text:
    pad = max(width - len(line), 0)
    left = pad // 2
    text = Text(" " * left)
    text.append_text(gradient_line(line, stops))
    text.append(" " * (pad - left))
    return text


def wordmark_lines() -> list[Text]:
    """Return the two stacked words as equally wide, centered Text lines."""
    rows: list[Text] = []
    for line in DEEPPYRO_ART:
        rows.append(_padded_line(line, FIRE_STOPS, ART_WIDTH))
    rows.append(Text(" " * ART_WIDTH))
    for line in HARNESS_ART:
        rows.append(_padded_line(line, ICE_STOPS, ART_WIDTH))
    return rows


def compact_wordmark_line() -> Text:
    """Single-line wordmark used by narrow terminals."""
    return gradient_line(COMPACT_WORD, FIRE_STOPS)


__all__ = [
    "ART_WIDTH",
    "COMPACT_WORD",
    "DEEPPYRO_ART",
    "FIRE_STOPS",
    "HARNESS_ART",
    "ICE_STOPS",
    "color_at",
    "compact_wordmark_line",
    "gradient_line",
    "wordmark_lines",
]
