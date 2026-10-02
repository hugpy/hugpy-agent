"""Screen geometry (h26 §3.2). Pure: (h, w, composer_lines) -> Rects.

80x24 is the floor. Below 80 cols or 20 rows the sidebar folds into the
header (`[K] C W L`) and the composer caps at 3 lines; at >= 120 cols the
sidebar widens to 28 so each row can carry its model.
"""
from __future__ import annotations

from typing import NamedTuple


class Rect(NamedTuple):
    y: int
    x: int
    h: int
    w: int

    @property
    def bottom(self):
        return self.y + self.h


class Rects(NamedTuple):
    header: Rect
    sidebar: Rect          # h == 0 when folded into the header
    transcript: Rect
    composer: Rect
    status: Rect
    narrow: bool
    wide: bool
    rule: Rect = Rect(0, 0, 0, 0)   # the composer's top border/title row


def composer_cap(h, w):
    return 3 if (w < 80 or h < 20) else 6


def compute(h, w, composer_lines=1):
    h, w = max(6, h), max(20, w)
    narrow, wide = (w < 80 or h < 20), w >= 120
    lines = max(1, min(int(composer_lines or 1), composer_cap(h, w)))
    header = Rect(0, 0, 1, w)
    status = Rect(h - 1, 0, 1, w)
    composer = Rect(h - 1 - lines, 0, lines, w)
    rule = Rect(composer.y - 1, 0, 1, w)
    body_h = max(1, rule.y - 1)
    if narrow:
        sidebar = Rect(1, 0, 0, 0)
        transcript = Rect(1, 0, body_h, w)
    else:
        side_w = 28 if wide else 20
        sidebar = Rect(1, 0, body_h, side_w)
        transcript = Rect(1, side_w + 1, body_h, w - side_w - 1)
    return Rects(header, sidebar, transcript, composer, status, narrow, wide, rule)
