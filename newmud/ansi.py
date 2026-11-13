"""ANSI / xterm color parsing.

Understands the 16 classic colors, xterm 256 colors (ESC[38;5;Nm) and
24-bit true color (ESC[38;2;R;G;Bm). Cursor movement and screen clearing
codes are stripped, since a MUD output window is a scrolling log.
"""
import re
from typing import NamedTuple, Optional

# Classic 16-color palette (normal 0-7, bright 8-15). Tuned to be readable on black.
DEFAULT_PALETTE = [
    "#000000", "#c50f1f", "#13a10e", "#c19c00", "#0037da", "#881798", "#3a96dd", "#cccccc",
    "#767676", "#e74856", "#16c60c", "#f9f1a5", "#3b78ff", "#b4009e", "#61d6d6", "#f2f2f2",
]

_CUBE = [0, 95, 135, 175, 215, 255]


def xterm_color(n: int, palette=DEFAULT_PALETTE) -> str:
    if n < 16:
        return palette[n]
    if n < 232:
        n -= 16
        r, g, b = _CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6]
    else:
        r = g = b = 8 + (n - 232) * 10
    return f"#{r:02x}{g:02x}{b:02x}"


class Style(NamedTuple):
    fg: Optional[object] = None  # None, int (0-255 palette index) or "#rrggbb"
    bg: Optional[object] = None
    bold: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    blink: bool = False


PLAIN = Style()

_CSI = re.compile(r"\x1b\[([0-9;:?<=>]*)([@-~])")
_ESC_OTHER = re.compile(r"\x1b[()*+][0-9A-Za-z]|\x1b[^\[]")
_PARTIAL = re.compile(r"\x1b(\[[0-9;:?<=>]*|[()*+])?$")


def _apply_sgr(style: Style, params: str) -> Style:
    fg, bg, bold, italic, underline, reverse, blink = style
    codes = [c for c in params.replace(":", ";").split(";")]
    if codes == [""]:
        codes = ["0"]
    i = 0
    while i < len(codes):
        try:
            c = int(codes[i] or 0)
        except ValueError:
            i += 1
            continue
        if c == 0:
            fg = bg = None
            bold = italic = underline = reverse = blink = False
        elif c == 1:
            bold = True
        elif c == 2 or c == 22:
            bold = False
        elif c == 3:
            italic = True
        elif c == 23:
            italic = False
        elif c == 4:
            underline = True
        elif c == 24:
            underline = False
        elif c in (5, 6):
            blink = True
        elif c == 25:
            blink = False
        elif c == 7:
            reverse = True
        elif c == 27:
            reverse = False
        elif 30 <= c <= 37:
            fg = c - 30
        elif c == 39:
            fg = None
        elif 40 <= c <= 47:
            bg = c - 40
        elif c == 49:
            bg = None
        elif 90 <= c <= 97:
            fg = c - 90 + 8
        elif 100 <= c <= 107:
            bg = c - 100 + 8
        elif c in (38, 48) and i + 1 < len(codes):
            color = None
            mode = codes[i + 1]
            if mode == "5" and i + 2 < len(codes):
                try:
                    color = int(codes[i + 2]) % 256
                except ValueError:
                    pass
                i += 2
            elif mode == "2" and i + 4 < len(codes):
                try:
                    r, g, b = (max(0, min(255, int(x or 0))) for x in codes[i + 2:i + 5])
                    color = f"#{r:02x}{g:02x}{b:02x}"
                except ValueError:
                    pass
                i += 4
            else:
                i += 1
            if color is not None:
                if c == 38:
                    fg = color
                else:
                    bg = color
        i += 1
    return Style(fg, bg, bold, italic, underline, reverse, blink)


class AnsiParser:
    """Stateful parser: feed text chunks, receive (text, Style) segments."""

    def __init__(self):
        self.style = PLAIN
        self._pending = ""

    def reset(self):
        self.style = PLAIN
        self._pending = ""

    def feed(self, text: str):
        text = self._pending + text
        self._pending = ""
        m = _PARTIAL.search(text)
        if m and len(text) - m.start() < 64:
            self._pending = text[m.start():]
            text = text[:m.start()]

        segments = []
        pos = 0
        style = self.style
        for m in _CSI.finditer(text):
            if m.start() > pos:
                segments.extend(self._plain(text[pos:m.start()], style))
            if m.group(2) == "m":
                style = _apply_sgr(style, m.group(1))
            pos = m.end()
        if pos < len(text):
            segments.extend(self._plain(text[pos:], style))
        self.style = style
        return [s for s in segments if s[0]]

    @staticmethod
    def _plain(chunk, style):
        chunk = _ESC_OTHER.sub("", chunk)
        chunk = chunk.replace("\r", "").replace("\x00", "").replace("\x07", "")
        if "\b" in chunk:
            out = []
            for ch in chunk:
                if ch == "\b":
                    if out:
                        out.pop()
                else:
                    out.append(ch)
            chunk = "".join(out)
        return [(chunk, style)]


def strip_ansi(text: str) -> str:
    return "".join(t for t, _ in AnsiParser().feed(text))


def resolve(style: Style, palette, default_fg, default_bg, bold_is_bright=True):
    """Turn a Style into concrete (fg_hex, bg_hex) for display."""
    fg, bg = style.fg, style.bg
    if isinstance(fg, int):
        if fg < 8 and style.bold and bold_is_bright:
            fg += 8
        fg = xterm_color(fg, palette)
    elif fg is None:
        fg = palette[15] if (style.bold and bold_is_bright and default_fg.lower() == palette[7].lower()) else default_fg
    if isinstance(bg, int):
        bg = xterm_color(bg, palette)
    if style.reverse:
        fg, bg = (bg or default_bg), fg
    return fg, bg
