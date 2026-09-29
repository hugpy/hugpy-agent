"""The eval task suite (plan P3.4).

The canonical definitions live in `hugpy_agent.eval.DEFAULT_TASKS` so the same
suite ships in the wheel and `hugpy-agent eval` runs it on any box. This module
re-exports them as the operator-editable surface — append your own `EvalTask`
to `TASKS` here (with a deterministic checker) to extend a local run.

Each task is deliberately small (short prompt, low step cap, small token
budget) so a live comparative run over several models has bounded GPU cost.
A task passes only when the run finished (`outcome == "done"`), stayed under
its step cap, AND its deterministic checker holds (the artifact appeared / the
answer contains the required fact) — never an LLM judging an LLM.

The built-in tasks:

  write_artifact         write answer.txt == "BLACKBIRD", then finish
                         (fs_write + final_answer)
  read_fact              read config.txt, report the launch code 4471
                         (fs_read + final_answer)
  glob_count             count the .log files (3) and state the number
                         (fs_glob + final_answer)
  read_transform_write   read input.txt, write its UPPERCASE into output.txt
                         (fs_read + fs_write, two-step)
"""
import os
import sys

# Standalone-run shim (mirrors scripts/live_smoke.py): make the src/ package
# importable when this file is run from a checkout without `pip install -e`.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if os.path.isdir(_SRC) and _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from hugpy_agent.eval import (  # noqa: E402
    DEFAULT_TASKS, EvalTask, CheckContext,
    answer_contains, file_contains, all_of,
)

# The suite the runner and CLI score against. Extend by appending local tasks:
#   TASKS = DEFAULT_TASKS + [EvalTask(name="...", prompt="...", check=...)]
TASKS = list(DEFAULT_TASKS)
