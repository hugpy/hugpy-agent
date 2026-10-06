"""End-to-end navigation tests for `hugpy-agent tui` driven over tmux.

Run:  python3 tests/tui_rig/run_tests.py
Exits non-zero if any check fails. Cleans up tmux + stub serve on exit.
"""
from __future__ import annotations

import sys

from tui_rig import harness as H


def no_tb(name, screen):
    H.check(name + " :: no traceback", "Traceback (most recent call last)" not in screen, screen)


def group_initial():
    s = H.capture()
    H.check("initial: header rendered (not splash)", s.startswith("hugpy-agent ") and "Connecting to" not in s, s)
    H.check("initial: header is provider-neutral", "claude" not in s.splitlines()[0].lower(), s)
    H.check("initial: status bar names the API, not a provider", "[abstract-serve " in s, s)
    H.check("initial: one token field", s.splitlines()[-1].count("tok ") + s.splitlines()[-1].count("ctx ") <= 1, s)
    H.check("initial: composer prompt visible", s.rstrip().splitlines()[-2].startswith(">") or "\n>" in s, s)
    H.check("initial: roster/roles shown", "ROLES" in s and "keeper" in s, s)
    H.check("initial: active role marked ●", "● keeper" in s, s)
    se = H.capture_e()
    H.check("initial: active role reverse-highlighted", any("keeper" in l for l in H.reversed_lines(se)), se)
    no_tb("initial", s)


def group_typing():
    H.literal("hello rig")
    s = H.capture()
    H.check("typing: text echoed in composer", "> hello rig" in s, s)
    H.keys("Enter")
    import time
    time.sleep(1.0)
    s = H.capture()
    H.check("typing: composer cleared after send", "> hello rig" not in s, s)
    H.check("typing: stream reply appeared", "got your prompt" in s or "stub stream" in s, s)
    no_tb("typing", s)


def group_slash():
    # open menu
    H.literal("/")
    s = H.capture()
    H.check("slash: menu lists commands", "/model" in s and "/status" in s, s)
    H.check("slash: selection marker ▸ shown", "▸" in s, s)
    H.check("slash: menu occludes transcript (first row is a menu row)",
            any(l.lstrip().startswith(("▸", "/")) or " ▸ " in l for l in s.splitlines()), s)
    # which row has the marker initially
    marked0 = _marked_cmd(H.capture())
    H.keys("Down")
    marked1 = _marked_cmd(H.capture())
    H.check("slash: Down moves ▸ marker", marked0 != marked1 and marked1 is not None, H.capture())
    H.keys("Up")
    marked2 = _marked_cmd(H.capture())
    H.check("slash: Up moves ▸ marker back", marked2 == marked0, H.capture())
    # Tab completes current selection into the buffer
    H.keys("Tab")
    s = H.capture()
    H.check("slash: Tab completes selection to composer", _composer_line(s).startswith("> /") and len(_composer_line(s)) > 3, s)
    # clear and reopen for Esc test
    _clear_composer()
    H.literal("/")
    H.keys("Escape")
    s = H.capture()
    H.check("slash: Esc closes the menu", "/model" not in _menu_region(s) and _composer_line(s).strip() in (">", ">"), s)
    # reopen for backspace test
    H.literal("/mo")
    s = H.capture()
    H.check("slash: prefix filters to /model", "/model" in s and "/status" not in _menu_region(s), s)
    H.keys("BSpace", "BSpace", "BSpace")
    s = H.capture()
    H.check("slash: backspace closes menu", _composer_line(s).strip() == ">", s)
    no_tb("slash", s)


def group_slash_execute():
    # Enter on a selection that is a plain command: /status writes a status note
    _clear_composer()
    H.literal("/status")
    H.keys("Enter")
    import time
    time.sleep(0.6)
    s = H.capture()
    H.check("slash: /status note appended to transcript", "status:" in s and "cursor" in s, s)
    no_tb("slash-exec", s)


def group_focus_and_select():
    H.keys("F2")
    se = H.capture_e()
    # a transcript block is reversed: its text AFTER the sidebar column │ is non-empty
    # (the sidebar role row reverses only text BEFORE the │).
    trans = [l.split("│")[-1].strip() for l in H.reversed_lines(se)
             if "│" in l and l.split("│")[-1].strip()]
    H.check("focus: F2 highlights a transcript block", len(trans) >= 1, se)
    before = H.reversed_lines(H.capture_e())
    H.keys("Up")
    after = H.reversed_lines(H.capture_e())
    H.check("focus: Up moves selection in transcript", before != after, H.capture_e())
    H.keys("Up", "Up")
    af2 = H.reversed_lines(H.capture_e())
    H.check("focus: further Up keeps moving selection", af2 != after, H.capture_e())
    # back to composer
    H.keys("F2")
    H.literal("x")
    s = H.capture()
    H.check("focus: F2 returns to composer (typing lands there)", "> x" in s, s)
    H.keys("BSpace")
    no_tb("focus", s)


def group_scroll():
    # Push scroll up with PgUp, expect a scroll indicator or changed top line
    top0 = _transcript_top(H.capture())
    H.keys("PPage")
    top1 = _transcript_top(H.capture())
    s = H.capture()
    H.check("scroll: PgUp scrolls transcript or shows ↓ indicator", top1 != top0 or "↓" in s, s)
    H.keys("C-u")
    H.keys("NPage")
    H.keys("End")
    s = H.capture()
    H.check("scroll: End returns to tail (last reply visible)",
            "TAIL reply at the very end." in s or "got your prompt" in s, s)
    no_tb("scroll", s)


def group_session_picker():
    H.keys("C-g")
    import time
    time.sleep(0.4)
    s = H.capture()
    H.check("picker: Ctrl-G opens SESSIONS modal", "SESSIONS" in s and ("keeper" in s.lower() or "chat" in s.lower()), s)
    H.keys("Down")
    H.keys("Enter")
    time.sleep(0.8)
    s = H.capture()
    H.check("picker: selecting a session returns to main view", "ROLES" in s, s)
    no_tb("picker", s)
    # /session <prefix>
    _clear_composer()
    H.literal("/session chat")
    H.keys("Enter")
    time.sleep(0.6)
    se = H.capture_e()
    H.check("picker: /session chat activates chat role",
            any("chat" in l for l in H.reversed_lines(se)), se)
    no_tb("session-prefix", se)


def group_role_cycle():
    import time
    se0 = H.capture_e()
    active0 = _active_role(se0)
    H.keys("Tab")
    time.sleep(0.5)
    active1 = _active_role(H.capture_e())
    H.check("role: Tab cycles active role", active0 != active1 and active1 is not None, H.capture_e())
    H.keys("BTab")
    time.sleep(0.5)
    active2 = _active_role(H.capture_e())
    H.check("role: Shift-Tab cycles back", active2 == active0, H.capture_e())
    no_tb("role-cycle", H.capture())


def group_model_picker():
    import time
    H.keys("C-p")
    time.sleep(0.5)
    s = H.capture()
    H.check("model: Ctrl-P opens MODEL picker", "MODEL" in s and ("Opus" in s or "Sonnet" in s or "Qwen" in s), s)
    H.keys("Escape")
    time.sleep(0.4)
    s = H.capture()
    H.check("model: Esc closes picker back to main", "ROLES" in s, s)
    no_tb("model-picker", s)


def group_context_status():
    import time
    _clear_composer()
    H.literal("/context")
    H.keys("Enter")
    time.sleep(0.8)
    s = H.capture()
    H.check("context: /context modal opens", "CONTEXT" in s, s)
    H.check("context: modal shows transcript content (USER/ASSISTANT)", "USER" in s or "ASSISTANT" in s, s)
    H.check("context: no 'events unavailable' error", "events unavailable" not in s, s)
    H.keys("NPage")
    H.keys("Escape")
    time.sleep(0.4)
    s = H.capture()
    H.check("context: Esc closes modal", "ROLES" in s and "CONTEXT" not in s, s)
    no_tb("context", s)


def group_help_tools():
    import time
    _clear_composer()
    H.literal("/help")
    H.keys("Enter")
    time.sleep(0.5)
    s = H.capture()
    H.check("help: /help modal opens", "HELP" in s and "Enter send" in s, s)
    H.check("help: lists the new commands", "/find" in s and "/export" in s and "/log" in s, s)
    H.keys("Escape")
    time.sleep(0.3)
    _clear_composer()
    H.literal("/tools")
    H.keys("Enter")
    time.sleep(0.5)
    s = H.capture()
    H.check("tools: /tools modal opens", "TOOLS" in s, s)
    H.keys("Escape")
    no_tb("help-tools", H.capture())


def group_freeze():
    """Auditor HIGH #1: after an Esc press the main loop must keep drawing (the
    Esc chord probe must restore timeout(100), never leave nodelay off)."""
    import time
    # make sure we're in the composer with an empty buffer
    s = H.capture()
    b0 = H.busy_seconds(s)
    H.check("freeze: BUSY counter present (precondition)", b0 is not None, s)
    H.keys("Escape", delay=0.3)        # bare Esc: used to freeze the loop
    time.sleep(1.6)                    # NO further keys — only the poll+draw loop can advance it
    s = H.capture()
    b1 = H.busy_seconds(s)
    H.check("freeze: screen still repaints ~1s after Esc (no freeze)",
            b1 is not None and b0 is not None and b1 > b0, s)
    no_tb("freeze", s)


def group_paste():
    """Auditor HIGH #3: a multi-line paste must insert literally, not submit the
    first line."""
    import time
    _clear_composer()
    H.paste("paste line one\npaste line two")
    s = H.capture()
    comp = "\n".join(l for l in s.splitlines() if l.startswith(">") or l.startswith("  "))
    H.check("paste: both pasted lines land in the composer",
            "paste line one" in s and "paste line two" in s, s)
    H.check("paste: paste did NOT submit (no 'paste line one' user turn sent)",
            "▶ paste line one" not in s, s)
    # clean the composer for later groups
    _clear_composer()
    H.keys(*(["BSpace"] * 40), delay=0.2)
    no_tb("paste", s)


def group_multiline_caret():
    """Auditor MED #4: Up/Down move the caret between composer lines."""
    import time
    _clear_composer()
    # build two lines with \+Enter (backslash newline)
    H.literal("alpha")
    H.keys("Escape", "Enter", delay=0.3)   # Alt+Enter newline
    H.literal("beta")
    s = H.capture()
    H.check("multiline: composer holds two lines", "alpha" in s and "beta" in s, s)
    H.keys("Up")                            # caret to line 1; typing should land there
    H.literal("X")
    s = H.capture()
    H.check("multiline: Up moves caret into first line (X lands on alpha line)",
            "alphaX" in s or "alpXha" in s or "Xalpha" in s or "alphX" in s or "alXpha" in s or "aXlpha" in s, s)
    _clear_composer()
    H.keys(*(["BSpace"] * 40), delay=0.2)
    no_tb("multiline", H.capture())


def group_home_top():
    """Auditor MED #6: Home in transcript focus scrolls to the top."""
    import time
    H.keys("End")
    H.keys("F2")            # focus transcript
    H.keys("Home")
    time.sleep(0.3)
    s = H.capture()
    H.check("home-top: Home in transcript shows the first events",
            "Hello first prompt" in s, s)
    H.keys("F2")            # back to composer
    no_tb("home-top", s)


def group_resize():
    import time
    H.resize(90, 30)
    time.sleep(0.4)
    s = H.capture()
    H.check("resize: narrow layout not corrupted", "hugpy" in s and "Traceback" not in s, s)
    H.resize(70, 24)
    time.sleep(0.4)
    s = H.capture()
    H.check("resize: <80 cols folds sidebar, still renders", "hugpy :" in s and "Traceback" not in s, s)
    H.resize(H.WIDTH, H.HEIGHT)
    time.sleep(0.4)
    s = H.capture()
    H.check("resize: restore wide layout", "ROLES" in s, s)
    no_tb("resize", s)


# -- small screen parsers --------------------------------------------------

def _composer_line(screen):
    for l in reversed(screen.splitlines()):
        if l.startswith(">"):
            return l
    return ""


def _clear_composer():
    # Caret to the very end (a multi-line buffer may hold the caret on line 1),
    # then backspace the buffer empty. (Ctrl-C would QUIT the app once the
    # buffer is already empty, so never use it to clear.)
    H.keys("Down", "Down", "Down", "End", delay=0.15)
    H.keys(*(["BSpace"] * 48), delay=0.25)


def _menu_region(screen):
    # lines above the composer rule that look like menu rows
    out = []
    for l in screen.splitlines():
        if l.lstrip().startswith(("▸", "/")) or " /" in l and ("  " in l):
            out.append(l)
    return "\n".join(out)


def _marked_cmd(screen):
    import re
    for l in screen.splitlines():
        m = re.search(r"▸\s+(/\w+)", l)   # the menu marker row: ' ▸ /command  desc'
        if m:
            return m.group(1)
    return None


def _transcript_top(screen):
    for l in screen.splitlines():
        if "│" in l:
            return l.split("│", 1)[1].strip()
    return ""


def _active_role(screen_e):
    for l in H.reversed_lines(screen_e):
        if l.startswith("hugpy-agent"):
            continue                                  # header locus tabs are reversed too
        l = l.split("│", 1)[0]                        # sidebar column only
        for role in ("keeper", "chat", "worker", "local"):
            if role in l:
                return role
    return None


def group_tools_out():
    """/find jumps, y copies (OSC 52 notice), /export writes, /log shows diagnostics."""
    import os
    import time
    _clear_composer()
    H.literal("/find TAIL reply")
    H.keys("Enter")
    time.sleep(0.6)
    s = H.capture()
    H.check("find: /find reports the hit", "find 'TAIL reply'" in s, s)
    se = H.capture_e()
    H.check("find: the matching block is selected", any("TAIL reply" in l for l in H.reversed_lines(se)), se)
    H.literal("y")
    time.sleep(0.4)
    s = H.capture()
    H.check("copy: y in transcript focus copies (OSC 52)", "copied" in s and "OSC 52" in s, s)
    H.keys("F2")
    _clear_composer()
    H.literal("/export")
    H.keys("Enter")
    time.sleep(0.6)
    s = H.capture()
    files = os.listdir(H.EXPORTS) if os.path.isdir(H.EXPORTS) else []
    H.check("export: notice names the file", "exported" in s, s)
    H.check("export: a private Markdown file exists",
            len(files) == 1 and (os.stat(os.path.join(H.EXPORTS, files[0])).st_mode & 0o077) == 0, s)
    _clear_composer()
    H.literal("/log")
    H.keys("Enter")
    time.sleep(0.6)
    s = H.capture()
    H.check("log: /log shows build + serve diagnostics", "LOG" in s and "hugpy-agent" in s and "serve   http" in s, s)
    H.keys("Escape")
    time.sleep(0.3)
    no_tb("tools-out", H.capture())


def group_permission(port):
    """A serve permission request on a cs- session opens a modal; y answers
    allow_once over /api/console/approval and the card shows the decision."""
    import time
    _clear_composer()
    H.literal("/session cs-dead")
    H.keys("Enter")
    time.sleep(1.5)
    s = H.capture()
    H.check("permission: cs- worker shows per-call context tokens", "ctx 31.2k" in s, s)
    H.stub_control(port, permission=True)
    time.sleep(2.0)
    s = H.capture()
    H.check("permission: modal opens for the request", "? Allow Bash" in s and "rm -rf build" in s, s)
    H.check("permission: keys name serve decisions", "allow for this session" in s, s)
    H.literal("y")
    time.sleep(2.0)
    approvals = H.stub_control(port).get("approvals") or []
    H.check("permission: y posted allow_once", approvals and approvals[-1].get("decision") == "allow_once"
            and approvals[-1].get("request_id") == "perm-rig", str(approvals))
    s = H.capture()
    H.check("permission: card shows the decision", "→ allowed once" in s, s)
    no_tb("permission", s)
    _clear_composer()
    H.literal("/session keeper")
    H.keys("Enter")
    time.sleep(0.8)


def group_chaos(port):
    """Malformed replies must not kill the poller: the UI keeps drawing, the
    fault is counted + logged, and polling recovers when the serve does."""
    import time
    H.stub_control(port, chaos="garbage")
    time.sleep(3.5)
    _clear_composer()
    H.literal("still typing")
    s = H.capture()
    H.check("chaos: UI still takes input under malformed replies", "> still typing" in s, s)
    H.check("chaos: the fault is surfaced (alert or notice)", "⚠" in s or "poll:" in s, s)
    H.stub_control(port, chaos="")
    time.sleep(3.0)
    _clear_composer()
    s = H.capture()
    H.check("chaos: recovers to live", "● live" in s, s)
    no_tb("chaos", s)


def group_outage(stub, port):
    """Serve dies: banner + retry countdown, input still works; serve comes
    back on the same port: live again without restarting the TUI."""
    import time
    stub.terminate()
    stub.wait(5)
    time.sleep(9.0)
    s = H.capture()
    H.check("outage: shows the serve is unreachable", "unreachable" in s or "✕ down" in s or "retrying" in s, s)
    H.literal("x")
    s = H.capture()
    H.check("outage: typing still echoes", "> x" in s, s)
    H.keys("BSpace")
    stub2, _ = H.start_stub(port)
    time.sleep(12.0)
    s = H.capture()
    H.check("outage: reconnects when the serve returns", "● live" in s, s)
    no_tb("outage", s)
    return stub2


def main():
    stub, port = H.start_stub()
    try:
        H.start_tui(port)
        group_initial()
        group_typing()
        group_slash()
        group_slash_execute()
        group_focus_and_select()
        group_scroll()
        group_session_picker()
        group_role_cycle()
        group_model_picker()
        group_context_status()
        group_help_tools()
        group_freeze()
        group_paste()
        group_multiline_caret()
        group_home_top()
        group_tools_out()
        group_permission(port)
        group_chaos(port)
        stub = group_outage(stub, port)
        group_resize()
    finally:
        H.kill_session()
        stub.terminate()
    passed = sum(1 for _, ok in H._RESULTS if ok)
    total = len(H._RESULTS)
    print("\n==== %d/%d checks passed ====" % (passed, total))
    fails = [n for n, ok in H._RESULTS if not ok]
    if fails:
        print("FAILURES:")
        for n in fails:
            print("  - " + n)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
