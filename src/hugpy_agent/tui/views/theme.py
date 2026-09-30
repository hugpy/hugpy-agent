"""Colour pairs (h26 §3.3), derived from the mct ANSI palette and the station
web palette. `use_default_colors()` so nothing paints a background — with
tmux `alternate-screen off` the scrollback stays readable. Without colours:
A_BOLD for user/header, A_DIM for muted/thinking, A_REVERSE for selection/held.
"""
from __future__ import annotations

import curses
from dataclasses import dataclass


@dataclass
class Theme:
    USER: int = 0
    ASSIST: int = 0
    THINK: int = 0
    TOOL: int = 0
    TOOL_ERR: int = 0
    OK: int = 0
    ACCENT: int = 0
    MUTED: int = 0
    HELD: int = 0
    SELECT: int = 0
    colours: bool = False


def plain():
    """No-colour fallback; also what tests use (no curses calls)."""
    return Theme(USER=curses.A_BOLD, ASSIST=0, THINK=curses.A_DIM, TOOL=0, TOOL_ERR=curses.A_BOLD,
                 OK=0, ACCENT=curses.A_BOLD, MUTED=curses.A_DIM, HELD=curses.A_REVERSE,
                 SELECT=curses.A_REVERSE, colours=False)


def init():
    try:
        if not curses.has_colors():
            return plain()
        curses.start_color()
        curses.use_default_colors()
        pairs = [(1, curses.COLOR_CYAN), (2, -1), (3, curses.COLOR_MAGENTA), (4, curses.COLOR_YELLOW),
                 (5, curses.COLOR_RED), (6, curses.COLOR_GREEN), (7, curses.COLOR_BLUE), (8, curses.COLOR_WHITE),
                 (9, curses.COLOR_RED)]
        for n, fg in pairs:
            curses.init_pair(n, fg, -1)
    except curses.error:
        return plain()
    cp = curses.color_pair
    return Theme(USER=cp(1), ASSIST=cp(2), THINK=cp(3) | curses.A_DIM, TOOL=cp(4), TOOL_ERR=cp(5), OK=cp(6),
                 ACCENT=cp(7) | curses.A_BOLD, MUTED=cp(8) | curses.A_DIM, HELD=cp(9) | curses.A_REVERSE,
                 SELECT=curses.A_REVERSE, colours=True)
