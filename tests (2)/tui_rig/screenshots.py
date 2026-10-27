"""Regenerate the README screenshots (docs/img/hugpy-agent-*.png) from the REAL
TUI. Reproducible and free of operator data:

    python3 tests/tui_rig/screenshots.py [--out docs/img] [--chrome /usr/bin/google-chrome]

Phase 1 (inside `unshare -rn`, a private network namespace, so the demo serve
can listen on 127.0.0.1:9124 without touching the host's serves): start
`stub_serve.py --demo` (demo_data.py), run `hugpy-agent tui` from ../../src in
tmux for each scene, capture the panes WITH colours (`capture-pane -e`).
Phase 2 (normal namespace): turn each capture into an HTML character grid and
screenshot it with headless Chrome via Playwright (device scale 2).

Needs: tmux, util-linux `unshare` with unprivileged user namespaces, Playwright
(python) + a Chrome/Chromium binary, DejaVu Sans Mono (Noto for symbols).
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC = os.path.join(ROOT, "src")
PORT = 9124

# (name, cols, rows, setup) — setup runs after the TUI is live
SCENES = [
    ("hugpy-agent-tui", 168, 27, "main"),
    ("hugpy-agent-approval", 112, 18, "approval"),
    ("hugpy-agent-splash", 100, 20, "splash"),
]

PALETTE = {  # dark theme; ANSI 0-15
    0: "#1d2026", 1: "#e06c75", 2: "#98c379", 3: "#e5c07b", 4: "#61afef", 5: "#c678dd", 6: "#56b6c2",
    7: "#d7dae0", 8: "#5c6370", 9: "#ef7b84", 10: "#a9d18a", 11: "#f0ce8c", 12: "#7cc0f5", 13: "#d48ce6",
    14: "#6ccad5", 15: "#ffffff",
}
BG, FG = "#15171c", "#d7dae0"


# -- phase 1: capture -------------------------------------------------------------

def _wait(pred, timeout=20.0, step=0.2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def _control(**body):
    req = urllib.request.Request("http://127.0.0.1:%d/__control" % PORT, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=3).read()


def capture(outdir):
    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    work = tempfile.mkdtemp(prefix="hugpy-shots-")
    home = os.path.join(work, "home")
    os.makedirs(os.path.join(home, ".hugpy"))
    with open(os.path.join(home, ".hugpy", "tui-loci.json"), "w") as fh:
        json.dump([{"locus": "keeper", "serve": "http://127.0.0.1:%d" % PORT},
                   {"locus": "hugpy", "serve": "http://127.0.0.1:9125"},
                   {"locus": "gpu-box-2", "ssh": "worker@gpu-box-2", "port": 9125}], fh)
    stub = subprocess.Popen([sys.executable, os.path.join(HERE, "stub_serve.py"), str(PORT), "--demo"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    stub.stdout.readline()
    sock = os.path.join(work, "tmux.sock")
    tmux = ["tmux", "-S", sock, "-f", "/dev/null"]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "TERM": "xterm-256color",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONPATH": SRC, "HUGPY_TUI_LOG": "off",
           "HUGPY_TOOLSERVER_URL": "http://127.0.0.1:%d" % PORT, "HUGPY_TUI_MOUSE": "0", "ESCDELAY": "25"}
    envs = " ".join("%s=%s" % (k, v) for k, v in env.items())
    cmd = "env -i %s %s -m hugpy_agent.cli tui --serve http://127.0.0.1:%d" % (envs, sys.executable, PORT)
    try:
        for name, cols, rows, scene in SCENES:
            subprocess.run(tmux + ["kill-server"], capture_output=True)
            _control(roster_delay=60 if scene == "splash" else 0)
            subprocess.run(tmux + ["new-session", "-d", "-x", str(cols), "-y", str(rows), cmd], check=True)

            def screen():
                return subprocess.run(tmux + ["capture-pane", "-p"], capture_output=True, text=True).stdout
            if scene == "splash":
                ok = _wait(lambda: "Connecting to" in screen())
            else:
                ok = _wait(lambda: "● live" in screen() and "tools: 259" in screen())
            if scene == "approval":
                _control(permission={"request_id": "perm-demo-2", "tool": "Bash",
                                     "input": {"command": "ssh gpu-box-2 sudo reboot",
                                               "description": "reboot gpu-box-2 (no heartbeat after the restart)"},
                                     "summary": "ssh gpu-box-2 sudo reboot", "risk": "destructive"})
                ok = _wait(lambda: "? Allow Bash" in screen())
            time.sleep(1.2)                                  # let one more poll settle the bar
            if not ok:
                raise SystemExit("scene %s never rendered:\n%s" % (name, screen()))
            ansi = subprocess.run(tmux + ["capture-pane", "-p", "-e"], capture_output=True, text=True).stdout
            with open(os.path.join(outdir, name + ".ansi"), "w") as fh:
                json.dump({"cols": cols, "rows": rows, "ansi": ansi}, fh)
            subprocess.run(tmux + ["kill-server"], capture_output=True)
            if scene == "splash":
                _control(roster_delay=0)
    finally:
        subprocess.run(tmux + ["kill-server"], capture_output=True)
        stub.terminate()
        shutil.rmtree(work, ignore_errors=True)


# -- phase 2: render ----------------------------------------------------------------

_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _colour(n):
    if n < 16:
        return PALETTE[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lv[n // 36], lv[n // 6 % 6], lv[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def _apply(state, params):
    p = [int(x) if x else 0 for x in params.split(";")] if params else [0]
    i = 0
    while i < len(p):
        c = p[i]
        if c == 0:
            state.update(fg=None, bg=None, bold=False, dim=False, rev=False)
        elif c == 1:
            state["bold"] = True
        elif c == 2:
            state["dim"] = True
        elif c == 22:
            state.update(bold=False, dim=False)
        elif c == 7:
            state["rev"] = True
        elif c == 27:
            state["rev"] = False
        elif 30 <= c <= 37:
            state["fg"] = _colour(c - 30)
        elif 90 <= c <= 97:
            state["fg"] = _colour(c - 90 + 8)
        elif c == 39:
            state["fg"] = None
        elif 40 <= c <= 47:
            state["bg"] = _colour(c - 40)
        elif 100 <= c <= 107:
            state["bg"] = _colour(c - 100 + 8)
        elif c == 49:
            state["bg"] = None
        elif c in (38, 48) and i + 1 < len(p):
            if p[i + 1] == 5 and i + 2 < len(p):
                state["fg" if c == 38 else "bg"] = _colour(p[i + 2])
                i += 2
            elif p[i + 1] == 2 and i + 4 < len(p):
                state["fg" if c == 38 else "bg"] = "#%02x%02x%02x" % tuple(p[i + 2:i + 5])
                i += 4
        i += 1


def _width(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def to_html(cols, rows, ansi):
    lines = ansi.split("\n")[:rows]
    lines += [""] * (rows - len(lines))
    out = []
    state = dict(fg=None, bg=None, bold=False, dim=False, rev=False)
    for line in lines:
        cells, pos = [], 0
        for m in re.finditer(r"\x1b\[[0-9;]*m|[^\x1b]", line):
            tok = m.group(0)
            if tok.startswith("\x1b"):
                _apply(state, _SGR.match(tok).group(1))
                continue
            w = _width(tok)
            if w == 0:
                continue
            cells.append((tok, w, dict(state)))
            pos += w
        while pos < cols:
            cells.append((" ", 1, dict(fg=None, bg=None, bold=False, dim=False, rev=False)))
            pos += 1
        row = []
        for ch, w, st in cells:
            fg, bg = st["fg"] or FG, st["bg"]
            if st["rev"]:
                fg, bg = (st["bg"] or BG), (st["fg"] or FG)
            style = "color:%s;" % fg
            if bg:
                style += "background:%s;" % bg
            if st["bold"]:
                style += "font-weight:700;"
            if st["dim"] and not st["rev"]:
                style += "opacity:.62;"
            cls = "c" if ch.isascii() else "c u"
            row.append('<span class="%s" style="%swidth:%dch">%s</span>' % (cls, style, w, html.escape(ch)))
        out.append('<div class="r">%s</div>' % "".join(row))
    return """<!doctype html><meta charset="utf-8"><style>
html,body{margin:0;background:#0e1013}
#t{display:inline-block;background:%s;padding:14px 16px;border-radius:8px;
   font:14.5px/1.32 "DejaVu Sans Mono","Noto Sans Mono",monospace;color:%s}
.r{white-space:pre;height:1.32em}
.c{display:inline-block;text-align:center;overflow:visible;vertical-align:top}
.u{font-family:"DejaVu Sans Mono","Noto Sans Symbols 2","Noto Sans Symbols","DejaVu Sans","Noto Color Emoji",monospace}
</style><div id="t">%s</div>""" % (BG, FG, "\n".join(out))


def render(outdir, chrome):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome, args=["--no-sandbox"])
        page = browser.new_page(device_scale_factor=2, viewport={"width": 2200, "height": 1400})
        for name, *_ in SCENES:
            src = os.path.join(outdir, name + ".ansi")
            with open(src) as fh:
                doc = json.load(fh)
            page.set_content(to_html(doc["cols"], doc["rows"], doc["ansi"]))
            page.locator("#t").screenshot(path=os.path.join(outdir, name + ".png"))
            os.remove(src)
            print("wrote", os.path.join(outdir, name + ".png"))
        browser.close()


def main():
    args = sys.argv[1:]
    outdir = os.path.abspath(args[args.index("--out") + 1]) if "--out" in args else os.path.join(ROOT, "docs", "img")
    chrome = args[args.index("--chrome") + 1] if "--chrome" in args else (
        shutil.which("google-chrome") or shutil.which("chromium") or "")
    os.makedirs(outdir, exist_ok=True)
    if os.environ.get("HUGPY_SHOTS_NS") == "1":
        capture(outdir)
        return
    subprocess.run(["unshare", "-rn", sys.executable, os.path.abspath(__file__), "--out", outdir],
                   env=dict(os.environ, HUGPY_SHOTS_NS="1"), check=True)
    render(outdir, chrome)


if __name__ == "__main__":
    main()
