"""Shell tool — subprocess with timeout, workspace cwd, output caps.

risk_class=destructive: an arbitrary command's side effects are unknowable,
so the journal will never blindly re-run one on resume, and Phase-2 policy
gates will hang an operator approval off this class.

The cwd jail is honest about its limits: cwd=workspace anchors relative
paths, but a shell can absolute-path anywhere the OS user can. Real
confinement is an OS concern (containers, users); the agent's contribution
is auditability (journaled before execution) + the risk class.
"""
from __future__ import annotations

import json
import subprocess

from . import RISK_DESTRUCTIVE, ToolSpec

OUTPUT_CAP = 16 * 1024      # bytes of stdout+stderr shown to the model
TIMEOUT_MAX = 300


def _cap(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + "\n[... truncated at %d bytes]" % cap


def spec(workspace: str) -> ToolSpec:
    def shell(command: str, timeout: int = 60) -> str:
        timeout = max(1, min(int(timeout), TIMEOUT_MAX))
        try:
            proc = subprocess.run(
                command, shell=True, cwd=workspace,
                capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "command timed out after %ds" % timeout,
                               "command": command})
        return json.dumps({
            "exit_code": proc.returncode,
            "stdout": _cap(proc.stdout or "", OUTPUT_CAP // 2),
            "stderr": _cap(proc.stderr or "", OUTPUT_CAP // 2),
        })

    return ToolSpec(
        name="shell",
        description=("Run a shell command with cwd = the workspace. Returns "
                     "exit_code, stdout, stderr (truncated at 8KB each). "
                     "Timeout default 60s, max 300s."),
        parameters={"type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {"type": "integer",
                                    "description": "seconds, default 60"}},
                    "required": ["command"]},
        handler=shell, risk_class=RISK_DESTRUCTIVE)
