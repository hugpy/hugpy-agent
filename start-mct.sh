#!/usr/bin/env bash
# start-mct.sh — start a NEW MCT conversation and show the rolling log in a
# second terminal (or a tmux pane if there's no GUI).
#
#   ./start-mct.sh                 # fresh workspace (new convo) under ~/.mct/
#   ./start-mct.sh /path/to/ws     # reuse/keep a specific workspace
#   MCT_MODEL=opus ./start-mct.sh  # pick A's model
#
set -u

MCT_DIR="${MCT_DIR:-/home/op/Desktop/mct}"
MODEL="${MCT_MODEL:-sonnet}"
# A fresh workspace = a new conversation. Pass an arg to reuse one instead.
WORKSPACE="${1:-$HOME/.mct/session-$(date +%Y%m%d-%H%M%S)}"

LOG_DIR="$WORKSPACE/.hugpy_agent/mct"
LOG="$LOG_DIR/mct.log"

if [ ! -d "$MCT_DIR/src/hugpy_agent/mct" ]; then
  echo "error: MCT not found at $MCT_DIR — set MCT_DIR=/path/to/mct" >&2
  exit 1
fi
cd "$MCT_DIR"
mkdir -p "$LOG_DIR"
: > "$LOG"        # create the log now so 'tail -F' attaches immediately

echo "workspace : $WORKSPACE"
echo "rolling log: $LOG"
echo

# Command the log window runs: header + follow the log.
TAIL_CMD="printf '\033[1mMCT rolling log\033[0m  %s\n(press Ctrl-C or close this window to stop watching)\n\n' \"$LOG\"; exec tail -n +1 -F \"$LOG\""

cleanup() { pkill -f "tail -n \+1 -F $LOG" 2>/dev/null || true; }
trap cleanup EXIT

# --- open the log viewer in a second terminal -------------------------------
launched=""
if [ -n "${DISPLAY:-}" ] && command -v gnome-terminal >/dev/null 2>&1; then
  gnome-terminal --title="MCT rolling log" -- bash -lc "$TAIL_CMD" >/dev/null 2>&1 & launched=gui
elif [ -n "${DISPLAY:-}" ] && command -v xterm >/dev/null 2>&1; then
  xterm -T "MCT rolling log" -bg black -fg green -e bash -lc "$TAIL_CMD" & launched=gui
elif [ -n "${DISPLAY:-}" ] && command -v x-terminal-emulator >/dev/null 2>&1; then
  x-terminal-emulator -e bash -lc "$TAIL_CMD" & launched=gui
elif command -v tmux >/dev/null 2>&1; then
  # No GUI: split a tmux window — log on top, conversation on the bottom.
  SESSION="mct-$$"
  tmux new-session -d -s "$SESSION" "bash -lc '$TAIL_CMD'"
  tmux split-window -v -t "$SESSION" \
    "cd '$MCT_DIR'; PYTHONPATH=src python3 tools/mct_repl.py '$WORKSPACE' --model '$MODEL'"
  tmux select-pane -t "$SESSION".1
  exec tmux attach -t "$SESSION"
fi

if [ -z "$launched" ]; then
  echo "No GUI terminal or tmux found. Open another terminal and run:"
  echo "    tail -n +1 -F \"$LOG\""
  printf "Then press Enter here to start the conversation... "
  read -r _
fi

# --- start the conversation in THIS terminal --------------------------------
PYTHONPATH=src python3 tools/mct_repl.py "$WORKSPACE" --model "$MODEL"
