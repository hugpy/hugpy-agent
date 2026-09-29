"""Optional installed CLI clients. Imported only when their profile is selected."""
import json
import os
import signal
import subprocess
import tempfile
import threading

from .profiles import binary


def stop_process(process):
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def run(profile, workspace, prompt, native_id, emit, control):
    kind = profile["protocol"]
    exe = binary(kind)
    if not exe:
        raise ValueError(kind + " is not installed on this client")
    if kind == "codex":
        cmd = [exe, "exec"]
        if native_id:
            cmd += ["resume", native_id]
        cmd += ["--json", "--skip-git-repo-check",
                "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"']
    else:
        cmd = [exe, "--print", "--verbose", "--output-format", "stream-json"]
        if native_id:
            cmd += ["--resume", native_id]
    if profile.get("model"):
        cmd += ["--model", profile["model"]]
    if kind == "codex":
        cmd.append("-")
    # stdin carries prompts, argv contains neither prompts nor credentials.
    # Native tools/MCP/auth stay owned by the installed client's configuration.
    with tempfile.TemporaryFile(mode="w+t") as errors:
        process = subprocess.Popen(cmd, cwd=workspace, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=errors, text=True,
                                   start_new_session=True)
        control["process"] = process
        timer = threading.Timer(int(profile.get("timeout", 1800)), stop_process, (process,))
        timer.daemon = True
        timer.start()
        answer = ""
        failed = False
        try:
            if control["stop"].is_set():
                stop_process(process)
            else:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    typ = event.get("type", "")
                    native_id = event.get("session_id") or event.get("thread_id") or native_id
                    if typ == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            answer = item.get("text", "")
                            emit("assistant", answer)
                        else:
                            emit("client", item)
                    elif typ == "assistant":
                        text = "".join(b.get("text", "") for b in event.get("message", {}).get("content", []) if b.get("type") == "text")
                        if text:
                            answer = text
                            emit("assistant", text)
                    elif typ == "result":
                        answer = event.get("result") or answer
                        failed = bool(event.get("is_error"))
                    elif typ in ("error", "turn.failed"):
                        failed = True
                        emit("client_error", event)
                process.wait()
        finally:
            timer.cancel()
            stop_process(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
            control.pop("process", None)
        if control["stop"].is_set():
            outcome = "interrupted"
        elif process.returncode or failed or not answer:
            outcome = "aborted"
        else:
            outcome = "done"
        return {"outcome": outcome, "answer": answer, "native_id": native_id,
                "error": "Native client did not complete; check its login and local configuration" if outcome == "aborted" else None}
