"""hugpy_agent — portable agent runtime on the hugpy fleet (Phase 1 MVP).

Layers (thin -> thick, per AGENT-SYSTEM-DESIGN.md §3):
  config   -> gateway (OpenAI-compat client) -> adapter (tool-calling)
  journal  (SQLite run ledger)  loop (assess->act->observe)
  tools    (registry + shell/fs/http/fleet)  memory (markdown facts)
  cli      (run | chat | resume | models)
"""

__version__ = "0.1.47"
