"""Operator-facing eval surface for hugpy-agent (plan P3.4).

The engine (task/checker types, readiness gate, scorecard math, rendering)
is the packaged, unit-tested module `hugpy_agent.eval`; this directory is the
surface a keeper actually touches:

  tasks.py    the task suite (re-exports the built-in DEFAULT_TASKS and is the
              place to add local tasks)
  runner.py   a top-level live runner that gates readiness, scores each model,
              and writes a scorecard to results/
  results/    scorecards (JSON + table); large runs are gitignored, the chosen
              summary is committed

`hugpy-agent eval --model … --model …` runs the same suite from the CLI.
"""
