#!/usr/bin/env python3
"""Live per-model eval runner (plan P3.4) — the top-level driver.

    python evals/runner.py [--model A --model B ...] [--out DIR] \
        [--ready-timeout SEC] [--ready-poll SEC]

Equivalent to `hugpy-agent eval` but runnable straight from a checkout (adds
src/ to the path, no install needed). Gates each model on a chat token-echo
readiness round-trip — NEVER on HTTP 200 or a serving mode flag, because a
down/loading worker answers 200 with an error body — then runs the suite and
writes a scorecard (JSON + table) to results/.

Config (base URL, API key, think knob) comes from the environment / the
workspace `.env`, exactly like the rest of the CLI. The key is never printed.
With no --model it scores the two operator-selected candidates for today.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hugpy_agent.config import load_config          # noqa: E402
from hugpy_agent import eval as evalmod              # noqa: E402
from tasks import TASKS                              # noqa: E402

# Today's operator-selected candidates (default when no --model is passed):
#   incumbent brain (Qwen3-Coder-Next since the 2026-07-17 brain switch —
#   reliability over speed, 4/4 vs klein's 3/4 on this very suite) vs. the
#   fast-but-flaky former default (a thinking-family brain: score it with
#   HUGPY_NO_THINK=true or it burns its budget inside <think>).
DEFAULT_MODELS = [
    "Qwen~Qwen3-Coder-Next-GGUF",
    "ponpoke/flux2-klein-9b-uncensored-text-encoder",
]


def _printer():
    def on_event(kind, *a):
        if kind == "eval_model":
            print("\n== scoring %s ==" % a[0], flush=True)
        elif kind == "eval_ready":
            print("  [ready] %s" % a[0], flush=True)
        elif kind == "eval_model_blocked":
            print("  [BLOCKED] %s: %s" % (a[0], a[1]), flush=True)
        elif kind == "eval_task":
            print("  [task] %-22s passed=%s outcome=%s steps=%s tok=%s"
                  % (a[0], a[1].get("passed"), a[1].get("outcome"),
                     a[1].get("steps"), a[1].get("est_tokens")), flush=True)
    return on_event


def main() -> int:
    ap = argparse.ArgumentParser(description="live per-model eval scorecard")
    ap.add_argument("--model", action="append", default=[],
                    help="model to score; repeatable (default: today's two)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__),
                                                  "results"))
    ap.add_argument("--ready-timeout", type=float, default=1200.0)
    ap.add_argument("--ready-poll", type=float, default=20.0)
    ap.add_argument("--no-ready-gate", action="store_true")
    args = ap.parse_args()

    models = args.model or DEFAULT_MODELS
    cfg = load_config()
    print("base=%s key=%s models=%s"
          % (cfg.base, "set" if cfg.api_key else "NOT SET", models), flush=True)

    poll = max(1.0, args.ready_poll)
    tries = max(1, int(args.ready_timeout / poll))
    cards = evalmod.run_scorecard(
        models, cfg, TASKS, ready_tries=tries, ready_poll=poll,
        gate_ready=not args.no_ready_gate, on_event=_printer())

    json_path, table_path = evalmod.write_results(cards, args.out)
    print("\n" + evalmod.format_table(cards), flush=True)
    print("\nwrote %s\n      %s" % (json_path, table_path), flush=True)
    return 0 if all(c.ready for c in cards) else 1


if __name__ == "__main__":
    sys.exit(main())
