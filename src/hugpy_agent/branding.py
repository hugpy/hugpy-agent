"""Rebrand the OpenCode wordmark to "Hugpy Agent" (operator ask, 2026-08-14).

OpenCode is the fleet's terminal face (see console.py), but the splash it
draws — the block-glyph "opencode" wordmark on the TUI home screen and the
CLI `M.logo()` banner — is baked into the compiled binary, not configurable.
This module patches those glyph tables in place.

Why this is safe:

  1. SAME-LENGTH PATCH. The binary is a bun-compiled ELF with embedded JS
     chunks; shifting any byte corrupts the chunk table. Every replacement
     region is padded with trailing spaces (harmless inside the glyph
     strings) to EXACTLY the original byte length, so no offset moves.
     The originals store glyphs as \\uXXXX escapes (6 bytes/glyph); we write
     raw UTF-8 (3 bytes/glyph), which is what buys the room for the longer
     wordmark.

  2. IDEMPOTENT + REVERSIBLE. A binary without the known "opencode" glyph
     rows is left untouched (already patched, or a future OpenCode whose
     layout we don't know — never guess). First patch keeps a one-time
     `<binary>.orig-logo` copy next to the target.

  3. COSMETIC ONLY, NEVER FATAL. console.launch() calls ensure_hugpy_logo()
     best-effort; unwritable binary (root-owned npm prefix), unknown
     version, any surprise -> the stock wordmark shows and the launch
     proceeds. Nothing here touches code paths, only string tables.

Patched sites (verified against opencode-ai 1.18.18):
  - `var O=[...]`            CLI logo, 4 rows spelling "opencode"
  - `{left:[...],right:[...]}` TUI home logo, "open"/"code" halves
    (two identical copies in different chunks)
  - the small `{left:o,right:c}` compact mark that follows each copy
"""
from __future__ import annotations

import os
import shutil

# Three-column letters, three ink rows. WHY 3-wide: the stock halves ("open"
# / "code") are 4 letters x 4 cols = 19 visual columns per half, and the TUI
# positions the logo from those exact row widths — 5-letter words at 3 cols
# (5*3 + 4 gaps = 19) keep every row's visual width IDENTICAL to stock, so
# the layout can't shift. Two-tone halves stay two words so the theme's
# left/right colors keep meaning: "Hugpy"+"Agent".
_CELLS = {
    "H": ("█ █", "█▀█", "▀ ▀"),
    "u": ("█ █", "█ █", "▀▀▀"),
    "g": ("█▀█", "█ █", "▀▀█"),
    "p": ("█▀█", "█ █", "█▀▀"),
    "y": ("█ █", "█ █", "▀▀█"),
    "A": ("▄▀▄", "█▀█", "▀ ▀"),
    "e": ("█▀█", "█▀▀", "▀▀▀"),
    "n": ("█▀▄", "█ █", "▀ ▀"),
    "t": ("▀█▀", " █ ", " ▀ "),
}


def _word(letters: str) -> list[str]:
    rows = [" ".join(_CELLS[c][r] for c in letters) for r in range(3)]
    return [" " * len(rows[0])] + rows          # blank ascender row on top

_LEFT = _word("Hugpy")
_RIGHT = _word("Agent")
assert all(len(r) == 19 for r in _LEFT + _RIGHT)   # stock geometry, exactly
_FULL = [l + " " + r for l, r in zip(_LEFT, _RIGHT)]
_MARK_LEFT = ["    ", "█  █", "█▀▀█", "▀  ▀"]    # compact mark: H / A
_MARK_RIGHT = ["    ", "▄▀▀▄", "█▀▀█", "▀  ▀"]


def logo_lines() -> list[str]:
    """Portable Hugpy Agent wordmark for consoles we control directly."""
    return list(_FULL)


def _js_array(rows: list[str]) -> bytes:
    return ("[" + ",".join('"' + r + '"' for r in rows) + "]").encode()


def _js_pair(left: list[str], right: list[str]) -> bytes:
    return (b"{left:" + _js_array(left) + b",right:" + _js_array(right) + b"}")


def _fit(replacement: bytes, target_len: int) -> bytes | None:
    """Pad `replacement` to target_len with spaces AFTER the closing bracket.
    JS ignores whitespace between tokens, so `var O=[...]    ;` stays valid
    while the glyph strings keep their exact visual width (padding inside a
    string would stretch the wordmark and wreck the TUI's centering). None if
    it can't fit, which aborts that site rather than corrupting it."""
    pad = target_len - len(replacement)
    if pad < 0:
        return None
    return replacement + b" " * pad


# ---- site locators (byte patterns of the stock 1.x wordmark) ---------------
def _esc(s: str) -> bytes:
    """The shipped chunks store glyphs as lowercase \\uXXXX escape TEXT —
    match that ASCII form, not raw UTF-8."""
    return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in s).encode()

# "open" row 1 as it appears escaped in the shipped chunks:
_ROW1 = _esc("█▀▀█ █▀▀█ █▀▀█ █▀▀▄")
_CLI_START = _esc('var O=["⠀')
_PAIR_START = _esc('{left:["                   ","') + _ROW1
_MARK_START = _esc('{left:["    ","█▀▀▀","█_^█"')


def _find_span(data: bytes, start_pat: bytes, open_at: int,
               close: bytes, offset: int = 0) -> tuple[int, int] | None:
    a = data.find(start_pat, offset)
    if a < 0:
        return None
    a += open_at
    b = data.find(close, a)
    if b < 0:
        return None
    return a, b + len(close)


def patch_bytes(data: bytes) -> tuple[bytes, int]:
    """Return (patched copy, number of sites patched). Unknown layout -> 0."""
    buf = bytearray(data)
    patched = 0

    # CLI logo: var O=[ ... "] — the array literal only
    span = _find_span(buf, _CLI_START, len(b"var O="), b'"]')
    if span:
        a, b = span
        rep = _fit(_js_array(["⠀" + " " * (len(_FULL[1]) - 1)] + _FULL[1:]), b - a)
        if rep:
            buf[a:b] = rep
            patched += 1

    # TUI logo pairs + compact marks (every copy)
    for start_pat, left, right in (
        (_PAIR_START, _LEFT, _RIGHT),
        (_MARK_START, _MARK_LEFT, _MARK_RIGHT),
    ):
        pos = 0
        while True:
            span = _find_span(buf, start_pat, 0, b"]}", pos)
            if span is None:
                break
            a, b = span
            rep = _fit(_js_pair(left, right), b - a)
            if rep:
                buf[a:b] = rep
                patched += 1
            pos = b

    return bytes(buf), patched


def ensure_hugpy_logo(binary: str) -> bool:
    """Idempotently rebrand `binary` in place. True if it is (now) branded.

    Reads the whole file (~180 MB — fine, this runs once per launch and only
    writes when stock glyphs are present). Keeps `<binary>.orig-logo` on the
    first patch. Any failure -> False, caller shows the stock wordmark.
    """
    try:
        with open(binary, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    if _ROW1 not in data:
        # already branded, or a layout we don't recognize — leave it alone
        return b"Hugpy" in data or "█▀▀█".encode() in data
    patched, count = patch_bytes(data)
    if not count:
        return False
    keep = binary + ".orig-logo"
    tmp = binary + ".brand-tmp"
    try:
        if not os.path.exists(keep):
            shutil.copy2(binary, keep)
        with open(tmp, "wb") as fh:
            fh.write(patched)
        shutil.copymode(binary, tmp)
        os.replace(tmp, binary)          # atomic: never a half-written exe
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True
